# encoding: utf-8
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.
#
# Author: Kyle Lahnakoski (kyle@lahnakoski.com)
#
from __future__ import absolute_import
from __future__ import division
from __future__ import unicode_literals

from mo_json import value2json
from mo_logs import constants, Log, startup
from mo_testing.fuzzytestcase import FuzzyTestCase
from mo_threads import Till

from mo_hg.hg_mozilla_org import HgMozillaOrg


class TestHg(FuzzyTestCase):

    @classmethod
    def setUpClass(cls):
        try:
            cls.config = startup.read_settings()
            constants.set(cls.config.constants)
            Log.start(cls.config.debug)
        except Exception as e:
            Log.error("Problem with etl", e)

    @classmethod
    def tearDownClass(cls):
        Log.stop()

    def setUp(self):
        self.hg = HgMozillaOrg(TestHg.config)

    def test_get_push(self):
        central = [b for b in self.hg.branches if b.name == "mozilla-central" and b.locale == "en-US"][0]
        test = self.hg._get_push(central, "b6b8e616de32")
        expected = {"date": 1503659542, "user": "archaeopteryx@coole-files.de", "id": 32390}
        self.assertEqual(test, expected)
        while len(self.hg.todo.queue):
            Till(seconds=1).wait()
