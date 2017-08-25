# encoding: utf-8
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.
#
# Author: Kyle Lahnakoski (kyle@lahnakoski.com)
#

from __future__ import division
from __future__ import unicode_literals

import re
from copy import copy

import mo_threads
from future.utils import text_type, binary_type
from mo_dots import set_default, Null, coalesce, unwraplist, listwrap
from mo_kwargs import override
from mo_logs import Log, strings, machine_metadata
from mo_logs.exceptions import Explanation, assert_no_exception, Except
from mo_logs.strings import expand_template
from mo_math.randoms import Random
from mo_threads import Thread, Lock, Queue, THREAD_STOP
from mo_threads import Till
from mo_times.dates import Date
from mo_times.durations import SECOND, Duration, HOUR, MINUTE, MONTH, DAY
from pyLibrary import convert
from pyLibrary.env import http, elasticsearch
from pyLibrary.meta import cache

from mo_hg.parse import diff_to_json
from mo_hg.repos.changesets import Changeset
from mo_hg.repos.pushs import Push
from mo_hg.repos.revisions import Revision, revision_schema

_hg_branches = None
_OLD_BRANCH = None


def _late_imports():
    global _hg_branches
    global _OLD_BRANCH

    from mo_hg import hg_branches as _hg_branches
    from mo_hg.hg_branches import OLD_BRANCH as _OLD_BRANCH

    _ = _hg_branches
    _ = _OLD_BRANCH


DEFAULT_LOCALE = "en-US"
DEBUG = True
DAEMON_DEBUG = True
DAEMON_INTERVAL = 30*SECOND
DAEMON_DO_NO_SCAN = ["try"]  # SOME BRANCHES ARE NOT WORTH SCANNING
MAX_TODO_AGE = DAY  # THE DAEMON WILL NEVER STOP SCANNING; DO NOT ADD OLD REVISIONS TO THE todo QUEUE

GET_DIFF = "{{location}}/raw-rev/{{rev}}"
GET_FILE = "{{location}}/raw-file/{{rev}}{{path}}"


last_called_url = {}


class HgMozillaOrg(object):
    """
    USE hg.mozilla.org FOR REPO INFORMATION
    USE ES AS A FASTER CACHE FOR THE SAME
    """

    @override
    def __init__(
        self,
        hg=None,        # CONNECT TO hg
        repo=None,      # CONNECTION INFO FOR ES CACHE
        branches=None,  # CONNECTION INFO FOR ES CACHE
        use_cache=False,   # True IF WE WILL USE THE ES FOR DOWNLOADING BRANCHES
        timeout=30 * SECOND,
        kwargs=None
    ):
        if not _hg_branches:
            _late_imports()

        self.es_locker = Lock()
        self.todo = mo_threads.Queue("todo")

        self.settings = kwargs
        self.timeout = Duration(timeout)

        if branches == None:
            self.branches = _hg_branches.get_branches(kwargs=kwargs)
            self.es = None
            return

        set_default(repo, {"schema": revision_schema})
        self.es = elasticsearch.Cluster(kwargs=repo).get_or_create_index(kwargs=repo)

        def setup_es(please_stop):
            self.es.add_alias()
            try:
                self.es.set_refresh_interval(seconds=1)
            except Exception:
                pass
        Thread.run("setup_es", setup_es)
        self.branches = _hg_branches.get_branches(kwargs=kwargs)

        Thread.run("hg daemon", self._daemon)

    def _daemon(self, please_stop):
        while not please_stop:
            with Explanation("looking for work"):
                branch, revisions = self.todo.pop(till=please_stop)
                if branch.name in DAEMON_DO_NO_SCAN:
                    continue
                revisions = set(revisions)

                # FIND THE REVSIONS ON THIS BRANCH
                for r in list(revisions):
                    with Explanation("Scanning {{branch}} {{revision|left(12)}}", branch=branch.name, revision=r, debug=DAEMON_DEBUG):
                        rev = self.get_revision(Revision(branch=branch, changeset={"id": r}))
                        if DAEMON_DEBUG:
                            Log.note("date {{date|datetime}}", date=rev.push.date)
                        revisions.discard(r)

                # FIND ANY BRANCH THAT MAY HAVE THIS REVISION
                for r in list(revisions):
                    self._find_revision(r)

    @cache(duration=HOUR, lock=True)
    def get_revision(self, revision, locale=None):
        """
        EXPECTING INCOMPLETE revision OBJECT
        RETURNS revision
        """
        rev = revision.changeset.id
        if not rev:
            return Null
        elif rev == "None":
            return Null
        elif revision.branch.name == None:
            return Null
        locale = coalesce(locale, revision.branch.locale, DEFAULT_LOCALE)
        output = self._get_from_elasticsearch(revision, locale=locale)
        if output:
            Log.note("Got hg ({{branch}}, {{locale}}, {{revision}}) from ES", branch=output.branch.name, locale=locale, revision=output.changeset.id)
            if output.push.date >= Date.now()-MAX_TODO_AGE:
                self.todo.add((output.branch, listwrap(output.parents)))
                self.todo.add((output.branch, listwrap(output.children)))
            return output

        found_revision = copy(revision)
        if isinstance(found_revision.branch, (text_type, binary_type)):
            lower_name = found_revision.branch.lower()
        else:
            lower_name = found_revision.branch.name.lower()

        if not lower_name:
            Log.error("Defective revision? {{rev|json}}", rev=found_revision.branch)

        b = found_revision.branch = self.branches[(lower_name, locale)]
        if not b:
            b = found_revision.branch = self.branches[(lower_name, DEFAULT_LOCALE)]
            if not b:
                Log.error("can not find branch ({{branch}}, {{locale}})", branch=lower_name, locale=locale)

        if Date.now() - Date(b.etl.timestamp) > _OLD_BRANCH:
            self.branches = _hg_branches.get_branches(kwargs=self.settings)

        push = self._get_push(found_revision.branch, found_revision.changeset.id)

        url = found_revision.branch.url.rstrip("/") + "/json-info?node=" + found_revision.changeset.id[0:12]
        with Explanation("get revision from {{url}}", url=url, debug=DEBUG):
            raw_rev = self._get_raw_revision(url, found_revision.branch)
            output = self._normalize_revision(raw_rev, found_revision, push)
            self.todo.add((output.branch, listwrap(output.parents)))
            self.todo.add((output.branch, listwrap(output.children)))
            return output

    def _get_from_elasticsearch(self, revision, locale=None):
        rev = revision.changeset.id
        query = {
            "query": {"filtered": {
                "query": {"match_all": {}},
                "filter": {"and": [
                    {"term": {"changeset.id12": rev[0:12]}},
                    {"term": {"branch.name": revision.branch.name}},
                    {"term": {"branch.locale": coalesce(locale, revision.branch.locale, DEFAULT_LOCALE)}}
                ]}
            }},
            "size": 2000
        }

        for attempt in range(3):
            try:
                docs = self.es.search(query).hits.hits
                break
            except Exception as e:
                e = Except.wrap(e)
                if "NodeNotConnectedException" in e:
                    # WE LOST A NODE, THIS MAY TAKE A WHILE
                    (Till(seconds=Random.int(5 * 60))).wait()
                    continue
                elif "EsRejectedExecutionException[rejected execution (queue capacity" in e:
                    (Till(seconds=Random.int(30))).wait()
                    continue
                else:
                    Log.warning("Bad ES call, fall back to TH", cause=e)
                    return None

        best = docs[0]._source
        if len(docs) > 1:
            for d in docs:
                if d._id.endswith(d._source.branch.locale):
                    best = d._source
            Log.warning("expecting no more than one document")

        if best.changeset.diff:
            return best
        elif not best.changeset.files:
            return best  # NOT EXPECTING A DIFF, RETURN IT ANYWAY
        else:
            return None

    @cache(duration=HOUR, lock=True)
    def _get_raw_revision(self, url, branch):
        raw_revs = self._get_and_retry(url, branch).values()
        if len(raw_revs) != 1:
            Log.error("do not know what to do")
        return raw_revs[0]

    @cache(duration=HOUR, lock=True)
    def _get_push(self, branch, changeset_id):
        try:
            # ALWAYS TRY ES FIRST
            response = self.es.search({
                "query": {"filtered": {
                    "query": {"match_all": {}},
                    "filter": {"and": [
                        {"term": {"branch.name": branch.name}},
                        {"prefix": {"changeset.id": changeset_id[0:12]}}
                    ]}
                }},
                "size": 1
            })
            json_push = response.hits.hits[0]._source.push
            return json_push
        except Exception:
            pass

        url = branch.url.rstrip("/") + "/json-pushes?full=1&changeset=" + changeset_id
        with Explanation("Pulling pushlog from {{url}}", url=url, debug=DEBUG):
            Log.note(
                "Reading pushlog from {{url}}",
                url=url,
                changeset=changeset_id
            )
            data = self._get_and_retry(url, branch)
            # QUEUE UP THE OTHER CHANGESETS IN THE PUSH
            self.todo.add((branch, [c.node for cs in data.values().changesets for c in cs]))
            pushes = [
                Push(id=int(index), date=_push.date, user=_push.user)
                for index, _push in data.items()
            ]
        if len(pushes) != 1:
            Log.error("do not know what to do")
        return pushes[0]

    def _normalize_revision(self, r, found_revision, push):
        new_names = set(r.keys()) - {"rev", "node", "user", "description", "date", "files", "backedoutby", "parents", "children", "branch", "tags"}
        if new_names and not r.tags:
            Log.warning("hg is returning new property names ({{names}})", names=new_names)

        rev = Revision(
            branch=found_revision.branch,
            index=r.rev,
            changeset=Changeset(
                id=r.node,
                id12=r.node[0:12],
                author=r.user,
                description=strings.limit(r.description, 2000),
                date=Date(r.date),
                files=r.files,
                backedoutby=r.backedoutby,
                bug=self._extract_bug_id(r.description)
            ),
            parents=unwraplist(r.parents),
            children=unwraplist(r.children),
            push=push,
            etl={"timestamp": Date.now().unix, "machine": machine_metadata}
        )
        # ADD THE DIFF
        rev.changeset.diff = self._get_json_diff_from_hg(rev)

        _id = coalesce(rev.changeset.id12, "") + "-" + rev.branch.name + "-" + coalesce(rev.branch.locale, DEFAULT_LOCALE)
        with self.es_locker:
            self.es.add({"id": _id, "value": rev})

        return rev

    def _get_and_retry(self, url, branch, **kwargs):
        """
        requests 2.5.0 HTTPS IS A LITTLE UNSTABLE
        """
        kwargs = set_default(kwargs, {"timeout": self.timeout.seconds})
        try:
            return _get_url(url, branch, **kwargs)
        except Exception as e:
            pass

        try:
            (Till(seconds=5)).wait()
            return _get_url(url.replace("https://", "http://"), branch, **kwargs)
        except Exception as f:
            pass

        path = url.split("/")
        if path[3] == "l10n-central":
            # FROM https://hg.mozilla.org/l10n-central/tr/json-pushes?full=1&changeset=a6eeb28458fd
            # TO   https://hg.mozilla.org/mozilla-central/json-pushes?full=1&changeset=a6eeb28458fd
            path = path[0:3] + ["mozilla-central"] + path[5:]
            return self._get_and_retry("/".join(path), branch, **kwargs)
        elif len(path) > 5 and path[5] == "mozilla-aurora":
            # FROM https://hg.mozilla.org/releases/l10n/mozilla-aurora/pt-PT/json-pushes?full=1&changeset=b44a8c68fc60
            # TO   https://hg.mozilla.org/releases/mozilla-aurora/json-pushes?full=1&changeset=b44a8c68fc60
            path = path[0:4] + ["mozilla-aurora"] + path[7:]
            return self._get_and_retry("/".join(path), branch, **kwargs)
        elif len(path) > 5 and path[5] == "mozilla-beta":
            # FROM https://hg.mozilla.org/releases/l10n/mozilla-beta/lt/json-pushes?full=1&changeset=03fbf7556c94
            # TO   https://hg.mozilla.org/releases/mozilla-beta/json-pushes?full=1&changeset=b44a8c68fc60
            path = path[0:4] + ["mozilla-beta"] + path[7:]
            return self._get_and_retry("/".join(path), branch, **kwargs)
        elif len(path) > 7 and path[5] == "mozilla-release":
            # FROM https://hg.mozilla.org/releases/l10n/mozilla-release/en-GB/json-pushes?full=1&changeset=57f513ab03308adc7aa02cc2ea8d73fe56ae644b
            # TO   https://hg.mozilla.org/releases/mozilla-release/json-pushes?full=1&changeset=57f513ab03308adc7aa02cc2ea8d73fe56ae644b
            path = path[0:4] + ["mozilla-release"] + path[7:]
            return self._get_and_retry("/".join(path), branch, **kwargs)
        elif len(path) > 5 and path[4] == "autoland":
            # FROM https://hg.mozilla.org/build/autoland/json-pushes?full=1&changeset=3ccccf8e5036179a3178437cabc154b5e04b333d
            # TO  https://hg.mozilla.org/integration/autoland/json-pushes?full=1&changeset=3ccccf8e5036179a3178437cabc154b5e04b333d
            path = path[0:3] + ["try"] + path[5:]
            return self._get_and_retry("/".join(path), branch, **kwargs)

        Log.error("Tried {{url}} twice.  Both failed.", {"url": url}, cause=[e, f])

    @cache(duration=HOUR, lock=True)
    def _find_revision(self, revision):
        please_stop = False
        locker = Lock()
        output = []
        queue = Queue("branches", max=2000)
        queue.extend(b for b in self.branches if b.locale == DEFAULT_LOCALE and b.name in ["try", "mozilla-inbound", "autoland"])
        queue.add(THREAD_STOP)

        problems = []
        def _find(please_stop):
            for b in queue:
                if please_stop:
                    return
                try:
                    url = b.url + "json-info?node=" + revision
                    rev = self.get_revision(Revision(branch=b, changeset={"id": revision}))
                    with locker:
                        output.append(rev)
                    Log.note("Revision found at {{url}}", url=url)
                except Exception as f:
                    problems.append(f)

        threads = []
        for i in range(3):
            threads.append(Thread.run("find changeset " + text_type(i), _find, please_stop=please_stop))

        for t in threads:
            with assert_no_exception:
                t.join()

        return output

    def _extract_bug_id(self, description):
        """
        LOOK INTO description to FIND bug_id
        """
        if description == None:
            return None
        match = re.findall(r'[Bb](?:ug)?\s*([0-9]{5,7})', description)
        if match:
            return int(match[0])
        return None

    def _get_json_diff_from_hg(self, revision):
        """
        :param revision: INCOMPLETE REVISION OBJECT 
        :return: 
        """
        @cache(duration=MINUTE, lock=True)
        def inner(changeset_id):
            try:
                # ALWAYS TRY ES FIRST
                response = self.es.search({
                    "query": {"filtered": {
                        "query": {"match_all": {}},
                        "filter": {"and": [
                            {"prefix": {"changeset.id": changeset_id[0:12]}}
                        ]}
                    }},
                    "size": 1
                })
                json_diff = response.hits.hits[0]._source.changeset.diff
                if json_diff:
                    return json_diff
            except Exception:
                pass

            url = expand_template(GET_DIFF, {"location": revision.branch.url, "rev": changeset_id})
            if DEBUG:
                Log.note("get unified diff from {{url}}", url=url)
            try:
                response = http.get(url)
                diff = response.content.decode("utf8", "replace")
                json_diff = diff_to_json(diff)
                if json_diff:
                    return json_diff
            except Exception as e:
                Log.warning("could not get unified diff", cause=e)

            return [{"new": f, "old": f} for f in revision.changeset.files]
        return inner(revision.changeset.id)

    def _get_source_code_from_hg(self, revision, file_path):
        response = http.get(expand_template(GET_FILE, {"location": revision.branch.url, "rev": revision.changeset.id, "path": file_path}))
        return response.content.decode("utf8", "replace")


def _trim(url):
    return url.split("/json-pushes?")[0].split("/json-info?")[0]


def _get_url(url, branch, **kwargs):
    with Explanation("get push from {{url}}", url=url, debug=DEBUG):
        response = http.get(url, **kwargs)
        data = convert.json2value(response.content.decode("utf8"))
        if isinstance(data, (text_type, str)) and data.startswith("unknown revision"):
            Log.error("Unknown push {{revision}}", revision=strings.between(data, "'", "'"))
        branch.url = _trim(url)  #RECORD THIS SUCCESS IN THE BRANCH
        return data


def minimize_repo(repo):
    repo.changeset.files = None
    repo.changeset.diff = None
    repo.changeset.description = strings.limit(repo.changeset.description, 1000)
    repo.etl = None
    repo.branch.last_used = None
    repo.branch.description = None
    repo.branch.etl = None
    repo.children = None
    repo.parents = None
