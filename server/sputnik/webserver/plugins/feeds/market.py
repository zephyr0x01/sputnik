#
# Copyright 2014 Mimetic Markets, Inc.
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
#

from sputnik import config
from sputnik import observatory
from sputnik import util

debug, log, warn, error, critical = observatory.get_loggers("feeds_market")

from sputnik.plugin import PluginException
from sputnik.webserver.plugin import ServicePlugin
from datetime import datetime

from twisted.internet.defer import inlineCallbacks, returnValue, gatherResults
from autobahn import wamp


class MarketAnnouncer(ServicePlugin):
    def encode_ticker(self, ticker):
        return ticker.replace("/", "_").lower()

    def on_trade(self, ticker, trade):
        ticker = self.encode_ticker(ticker)
        self.publish(u"feeds.market.trades.%s" % ticker, trade)

    def on_book(self, ticker, book):
        ticker = self.encode_ticker(ticker)
        self.publish(u"feeds.market.book.%s" % ticker, book)

    def on_safe_prices(self, ticker, price):
        ticker = self.encode_ticker(ticker)
        self.publish(u"feeds.market.safe_prices.%s" % ticker, price)

    def on_ohlcv(self, ticker, ohlcv):
        ticker = self.encode_ticker(ticker)
        self.publish(u"feeds.market.ohlcv.%s" % ticker, ohlcv)

