#
# Copyright 2014 Mimetic Markets, Inc.
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
#

import sys
import os

from twisted.internet.defer import maybeDeferred
from twisted.internet import task
import datetime

from test_sputnik import fix_config, TestSputnik

sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "../server"))
sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "../tools"))

fix_config()

from sputnik import ledger
from sputnik import models
from sputnik import util
from sputnik.exception import LedgerException

class TestLedger(TestSputnik):
    def setUp(self):
        TestSputnik.setUp(self)
        self.ledger = ledger.Ledger(self.session.bind.engine)
        self.export = ledger.AccountantExport(self.ledger)
        self.clock = task.Clock()

    def test_post_sequentially(self):
        post1 = {"uid":"foo", "count":2, "type":"Trade", "username":"customer",
                 "contract":"MXN", "quantity":5, "direction":"debit", "note": "test_debit",
                 "timestamp": util.dt_to_timestamp(datetime.datetime.utcnow())}
        post2 = {"uid":"foo", "count":2, "type":"Trade", "username":"customer",
                 "contract":"MXN", "quantity":5, "direction":"credit", "note": "test_credit",
                 "timestamp": util.dt_to_timestamp(datetime.datetime.utcnow())}
        d1 = self.export.post(post1)
        return self.export.post(post2)

    def test_post_results_agree(self):
        post1 = {"uid":"foo", "count":2, "type":"Trade", "username":"customer",
                 "contract":"MXN", "quantity":5, "direction":"debit", "note": 'debit',
                 "timestamp": util.dt_to_timestamp(datetime.datetime.utcnow())}
        post2 = {"uid":"foo", "count":2, "type":"Trade", "username":"customer",
                 "contract":"MXN", "quantity":5, "direction":"credit", "note": "credit",
                 "timestamp": util.dt_to_timestamp(datetime.datetime.utcnow())}
        d1 = self.export.post(post1)
        d2 = self.export.post(post2)
        return self.assertEqual(self.successResultOf(d1),
                self.successResultOf(d2))

    def test_post_simultaneously(self):
        post1 = {"uid":"foo", "count":2, "type":"Trade", "username":"customer",
                 "contract":"MXN", "quantity":5, "direction":"debit", "note": 'debit',
                 "timestamp": util.dt_to_timestamp(datetime.datetime.utcnow())}
        post2 = {"uid":"foo", "count":2, "type":"Trade", "username":"customer",
                 "contract":"MXN", "quantity":5, "direction":"credit", "note": 'credit',
                 "timestamp": util.dt_to_timestamp(datetime.datetime.utcnow())}
        return self.export.post(post1, post2)

    def test_database_commit(self):
        post1 = {"uid":"foo", "count":2, "type":"Trade", "username":"customer",
                 "contract":"MXN", "quantity":5, "direction":"debit", "note": "debit",
                 "timestamp": util.dt_to_timestamp(datetime.datetime.utcnow())}
        post2 = {"uid":"foo", "count":2, "type":"Trade", "username":"customer",
                 "contract":"MXN", "quantity":5, "direction":"credit", "note": "credit",
                 "timestamp": util.dt_to_timestamp(datetime.datetime.utcnow())}
        d = self.export.post(post1, post2)

        def dbtest(arg):
            postings = self.session.query(models.Posting).all()
            self.assertEqual(len(postings), 2)
            journals = self.session.query(models.Journal).all()
            self.assertEqual(len(journals), 1)
            p1 = postings[0]
            p2 = postings[1]
            journal = journals[0]
            self.assertEqual(p1.journal_id, journal.id)
            self.assertEqual(p2.journal_id, journal.id)
            self.assertEqual(abs(p1.quantity), 5)
            self.assertEqual(p1.quantity + p2.quantity, 0)

        return d.addCallback(dbtest)

    def test_count_mismatch(self):
        post1 = {"uid":"foo", "count":2, "type":"Trade", "username":"customer",
                 "contract":"MXN", "quantity":5, "direction":"debit", "note": "debit",
                 "timestamp": util.dt_to_timestamp(datetime.datetime.utcnow())}
        post2 = {"uid":"foo", "count":1, "type":"Trade", "username":"customer",
                 "contract":"MXN", "quantity":5, "direction":"credit", "note": "credit",
                 "timestamp": util.dt_to_timestamp(datetime.datetime.utcnow())}
        d1 = self.export.post(post1)
        d1.addErrback(lambda x: None)
        d2 = self.assertFailure(self.export.post(post2),
                LedgerException)
        self.flushLoggedErrors()
        return self.assertEqual(self.successResultOf(d2),
                ledger.COUNT_MISMATCH)

    def test_type_mismatch(self):
        post1 = {"uid":"foo", "count":2, "type":"Trade", "username":"customer",
                 "contract":"MXN", "quantity":5, "direction":"debit", "note": "debit",
                 "timestamp": util.dt_to_timestamp(datetime.datetime.utcnow())}
        post2 = {"uid":"foo", "count":2, "type":"Deposit", "username":"customer",
                 "contract":"MXN", "quantity":5, "direction":"credit", "note": "credit",
                 "timestamp": util.dt_to_timestamp(datetime.datetime.utcnow())}
        d1 = self.export.post(post1)
        d1.addErrback(lambda x: None)
        d2 = self.assertFailure(self.export.post(post2),
                LedgerException)
        self.flushLoggedErrors()
        return self.assertEqual(self.successResultOf(d2),
                ledger.TYPE_MISMATCH)

    def test_quantity_mismatch(self):
        post1 = {"uid":"foo", "count":2, "type":"Trade", "username":"customer",
                 "contract":"MXN", "quantity":5, "direction":"debit", "note": 'debit',
                 "timestamp": util.dt_to_timestamp(datetime.datetime.utcnow())}
        post2 = {"uid":"foo", "count":2, "type":"Trade", "username":"customer",
                 "contract":"MXN", "quantity":1, "direction":"credit", "note": 'credit',
                 "timestamp": util.dt_to_timestamp(datetime.datetime.utcnow())}
        d1 = self.export.post(post1)
        d1.addErrback(lambda x: None)
        d2 = self.assertFailure(self.export.post(post2),
                LedgerException)
        self.flushLoggedErrors()
        return self.assertEqual(self.successResultOf(d2),
                ledger.QUANTITY_MISMATCH)

    def test_timeout(self):
        post1 = {"uid":"foo", "count":2, "type":"Trade", "username":"customer",
                 "contract":"MXN", "quantity":5, "direction":"debit", "note": 'debit',
                 "timestamp": util.dt_to_timestamp(datetime.datetime.utcnow())}
        d1 = self.assertFailure(self.export.post(post1),
                LedgerException)
        group = self.ledger.pending["foo"]
        group.callLater = self.clock.callLater
        group.setTimeout(1)
        self.clock.advance(2)

        return self.assertEqual(self.successResultOf(d1),
                ledger.GROUP_TIMEOUT)

