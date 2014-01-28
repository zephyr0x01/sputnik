#!/usr/bin/env python

"""
Main websocket server, accepts RPC and subscription requests from clients. It's the backbone of the project,
facilitating all communications between the client, the database and the matching engine.
"""

from optparse import OptionParser

import config


parser = OptionParser()
parser.add_option("-c", "--config", dest="filename",
                  help="config file", default="../config/sputnik.ini")
(options, args) = parser.parse_args()

if options.filename:
    config.reconfigure(options.filename)

import cgi
import json
import logging
import sys
import datetime
import time
import onetimepass as otp
import hashlib
import uuid

from jsonschema import validate
from twisted.python import log
from twisted.internet import reactor, ssl
from twisted.web.server import Site
from twisted.web.static import File
from autobahn.websocket import listenWS
from autobahn.wamp import exportRpc, \
    WampCraProtocol, \
    WampServerFactory, \
    WampCraServerProtocol, exportSub, exportPub

from OpenSSL import SSL

from txzmq import ZmqFactory, ZmqEndpoint, ZmqPushConnection, ZmqPullConnection

zf = ZmqFactory()

#import database as db
#import models
if config.get("database", "uri").startswith("postgres"):
    import txpostgres as adbapi
else:
    import twisted.enterprise.adbapi as adbapi

dbpool = adbapi.ConnectionPool(config.get("database", "uri"))


class PublicInterface:
    def __init__(self, protocol):
        self.protocol = protocol

    @exportRpc
    def list_markets(self):
        return map(lambda x: x.dump(), self.protocol.factory.markets)

    @exportRpc
    def get_order_book(self):
        pass


class PrivateInterface:
    def __init__(self, protocol):
        self.protocol = protocol


MAX_TICKER_LENGTH = 100


def limit(func):
    last_called = [0.0]

    def kick(self, *arg, **kwargs):
        elapsed = time.clock() - last_called[0]

        if elapsed < self.RATE_LIMIT:
            self.count += 1
        else:
            # forgive past floods
            self.count -= 1

        last_called[0] = time.clock()

        if self.count > 100:
            WampCraServerProtocol.dropConnection(self)
            WampCraServerProtocol.connectionLost(self,
                                                 "rate limit exceeded")
        else:
            return func(self, *arg, **kwargs)

    return kick


class PepsiColaServerProtocol(WampCraServerProtocol):
    """
    Authenticating WAMP server using WAMP-Challenge-Response-Authentication ("WAMP-CRA").
    """

    def __init__(self):
        self.cookie = ""
        # rate limit counter
        self.count = 0
        self.username = None

    RATE_LIMIT = 0.5

    def connectionMade(self):
        """
        Called when a connection to the protocol is made
        this is the right place to initialize stuff, not __init__()
        """
        WampCraServerProtocol.connectionMade(self)

    def connectionLost(self, reason):
        """
        triggered when the connection is lost
        :param reason: reason why the connection was lost
        """
        logging.info("Connection was lost: %s" % reason)
        self.session.close()

    def onSessionOpen(self):
        """
        callback performed when a session is opened,
        it registers the client to a sample pubsub topic
        and overrides some global options
        """

        logging.info("in session open")
        ## register a single, fixed URI as PubSub topic

        self.registerForPubSub("wss://sputnikmkt.com:8000/safe_prices#", pubsub=WampCraServerProtocol.SUBSCRIBE,
                               prefixMatch=True)
        self.registerForPubSub("wss://sputnikmkt.com:8000/trades#", pubsub=WampCraServerProtocol.SUBSCRIBE,
                               prefixMatch=True)
        self.registerForPubSub("wss://sputnikmkt.com:8000/order_book#", pubsub=WampCraServerProtocol.SUBSCRIBE,
                               prefixMatch=True)
        self.registerForRpc(self, 'wss://sputnikmkt.com:8000/procedures/',
                            methods=[PepsiColaServerProtocol.make_account])
        self.registerForRpc(self, 'wss://sputnikmkt.com:8000/procedures/',
                            methods=[PepsiColaServerProtocol.list_markets])
        self.registerForRpc('wss://sputnikmkt.com:8000/procedures/',
                            methods=[PepsiColaServerProtocol.get_trade_history])
        self.registerForRpc(self, 'wss://sputnikmkt.com:8000/procedures/',
                            methods=[PepsiColaServerProtocol.get_order_book])

        # TODO: move this to onAuthenticated
        self.registerForRpc(self, 'wss://sputnikmkt.com:8000/procedures/',
                            methods=[PepsiColaServerProtocol.get_chat_history])
        self.registerForPubSub("wss://sputnikmkt.com:8000/user/chat", pubsub=WampCraServerProtocol.SUBSCRIBE,
                               prefixMatch=True)

        # override global client auth options
        self.clientAuthTimeout = 0
        self.clientAuthAllowAnonymous = True

        # call base class method
        WampCraServerProtocol.onSessionOpen(self)


    def getAuthPermissions(self, authKey, authExtra):
        """
        Gets the permission for a login... for now it's very basic
        :param authKey: pretty much the login
        :param authExtra: extra information, like a HMAC
        :return: the permissions associated with that user
        """
        print 'getAuthPermissions'

        if authKey in self.factory.cookies:
            username = self.factory.cookies[authKey]
        else:
            username = authKey

        def permission_callback(result):
            if result:
                salt, password_hash = result[0][0].password.split(":")
                authextra = {'salt': salt, 'keylen': 32, 'iterations': 1000}
            else:
                noise = hashlib.md5("super secret" + username + "even more secret")
                salt = noise.hexdigest()[:8]
                authextra = {'salt': salt, 'keylen': 32, 'iterations': 1000}

            # TODO: clean up permissions
            return {'permissions':
                        {'pubsub': [
                            {'uri': 'wss://sputnikmkt.com:8000/safe_price#%s' % 'USD.13.7.31',
                             'prefix': True,
                             'pub': False,
                             'sub': True},
                            {'uri': 'wss://sputnikmkt.com:8000/user/open_orders#%s' % username,
                             'prefix': True,
                             'pub': False,
                             'sub': True},
                            {'uri': 'wss://sputnikmkt.com:8000/user/fills#%s' % username,
                             'prefix': True,
                             'pub': False,
                             'sub': True},
                            {'uri': 'wss://sputnikmkt.com:8000/user/cancels#%s' % username,
                             'prefix': True,
                             'pub': False,
                             'sub': True}], 'rpc': []},
                    'authextra': authextra}

        return dbpool.runQuery("SELECT password FROM users WHERE username = ? LIMIT 1", (username,)). \
            addCallback(permission_callback)

    def getAuthSecret(self, authKey):
        """
        :param authKey: the login
        :return: the auth secret for the given auth key or None when the auth key
        does not exist
        """
        # check for a saved session
        if authKey in self.factory.cookies:
            return WampCraProtocol.deriveKey("cookie", {'salt': "cookie", 'keylen': 32, 'iterations': 1000})

        def auth_secret_callback(result):
            if not result:
                raise Exception("No such user: %s" % authKey)

            salt, secret = result[0][0].password.split(":")
            totp = result[0][1]

            try:
                otp_num = otp.get_totp(totp)
            except TypeError:
                otp_num = ""
            otp_num = str(otp_num)

            # hash password again but this in mostly unnecessary
            # totp should be safe enough to send over in the clear

            # TODO: extra hashing is being done with a possibly empty salt
            # does this weaken the original derived key?
            if otp_num:
                auth_secret = WampCraProtocol.deriveKey(secret, {'salt': otp_num, 'keylen': 32, 'iterations': 10})
            else:
                auth_secret = secret

            logging.info("returning auth secret: %s" % auth_secret)
            return auth_secret

        def auth_secret_errback(fail=None):
            logging.warning("Error retrieving auth secret: %s" % fail if fail else "Error retrieving auth secret")
            # WampCraProtocol.deriveKey returns base64 encoded data. Since ":"
            # is not in the base64 character set, this can never be a valid
            # password
            #
            # However, if this is discovered, someone can use it to sign
            # messages and authenticate as a nonexistent user
            # TODO: patch autobahn to prevent this without having to leak
            # information about user existence
            return ":0xFA1CDA7A:"

        return dbpool.runQuery(
            "SELECT password, totp FROM users WHERE username = ? LIMIT 1", (authKey,)
        ).addCallback(auth_secret_callback).addErrback(auth_secret_errback)

    # noinspection PyMethodOverriding
    def onAuthenticated(self, authKey, perms):
        """
        fired when authentication succeeds, registers user for RPC, save user object in session
        :param authKey: login
        :rtype : object
        :param perms: a dictionary describing the permissions associated with this user... from getAuthPermissions
        """

        self.troll_throttle = time.time()

        # based on what pub/sub we're permitted to register for, register to those
        self.registerForPubSubFromPermissions(perms['permissions'])

        ## register RPC endpoints (for now do that manually, keep in sync with perms)
        if perms is not None:
            # noinspection PyTypeChecker
            self.registerForRpc(self, baseUri="wss://sputnikmkt.com:8000/procedures/")

        # sets the user in the session...
        # search for a saved session
        username = self.factory.cookies.get(authKey)
        if not username:
            logging.info("Normal user login for: %s" % authKey)
            self.username = authKey
            uid = str(uuid.uuid4())
            self.factory.cookies[uid] = authKey
            self.cookie = uid
        else:
            logging.info("Cookie login for: %s" % username)
            self.username = username

        # moved from onSessionOpen
        # should the registration of these wait till after onAuth?  And should they only be for the specifc user?
        #  Pretty sure yes.
        self.registerForPubSub("wss://sputnikmkt.com:8000/user/cancels#" + username,
                               pubsub=WampCraServerProtocol.SUBSCRIBE)
        self.registerForPubSub("wss://sputnikmkt.com:8000/user/fills#" + username,
                               pubsub=WampCraServerProtocol.SUBSCRIBE)
        self.registerForPubSub("wss://sputnikmkt.com:8000/user/open_orders#" + username,
                               pubsub=WampCraServerProtocol.SUBSCRIBE)
        self.registerHandlerForPubSub(self, baseUri="wss://sputnikmkt.com:8000/user/")


    @exportRpc("get_cookie")
    @limit
    def get_cookie(self):
        return self.cookie

    @exportRpc("logout")
    @limit
    def logout(self):
        if self.cookie in self.factory.cookies:
            del self.factory.cookies[self.cookie]
        self.dropConnection()

    @exportRpc("get_new_two_factor")
    @limit
    def get_new_two_factor(self):
        """
        prepares new two factor authentication for an account
        """
        raise NotImplementedError()


    @exportRpc("disable_two_factor")
    @limit
    def disable_two_factor(self, confirmation):
        """
        disables two factor authentication for an account
        """
        raise NotImplementedError()

    @exportRpc("register_two_factor")
    @limit
    def register_two_factor(self, confirmation):
        """
        registers two factor authentication for an account
        :param secret: secret to store
        :param confirmation: trial run of secret
        """
        raise NotImplementedError()


    @exportRpc("get_trade_history")
    @limit
    def get_trade_history(self, ticker, time_span):
        """
        Gets a list of trades between two dates
        :param ticker: ticker of the contract to get the trade history from
        :param time_span: time span in seconds to look at
        """
        # sanitize input
        ticker_schema = {"type": "string"}
        validate(ticker, ticker_schema)
        time_span_schema = {"type": "number"}
        validate(time_span, time_span_schema)

        time_span = int(time_span)
        time_span = min(max(time_span, 0), 365 * 24 * 3600)
        ticker = ticker[:MAX_TICKER_LENGTH]

        to_dt = datetime.datetime.utcnow()
        from_dt = to_dt - datetime.timedelta(seconds=time_span)

        #todo implement time_span checks
        return dbpool.runQuery("SELECT trade.timestamp, trade.price, trade.quantity FROM trades JOIN contracts "
                               "WHERE contracts.ticker = ?" % (ticker,))

    @exportRpc("get_new_address")
    @limit
    def get_new_address(self):
        """
        assigns a new deposit address to a user and returns the address
        :return: the new address
        """

        def _update_new_address(txn, param):
            address_id, address = param[0], param[1]
            txn.execute("UPDATE addresses SET active=FALSE WHERE username=?", (self.username,))
            txn.execute("UPDATE addresses SET active=TRUE, username=? WHERE id=?", (self.username, address_id))
            return address

        def _get_new_address_callback(result):
            if not result:
                logging.error("Out of addresses!")
                raise Exception("Out of addresses")
            return dbpool.runInteraction(_update_new_address, result[0])

        return dbpool.runQuery(
            "SELECT id, address FROM addresses WHERE username=NULL AND active=TRUE ORDER BY id LIMIT 1").addCallback(
                _get_new_address_callback)

    @exportRpc("get_current_address")
    @limit
    def get_current_address(self):
        """
        RPC call to obtain the current address associated with a particular user
        :return: said current address
        """
        def _cb(result):
            if not result:
                logging.warning("we did not manage to get the current address associated with a user, something's wrong")
                return ""
            else:
                return result[0][0]
        return dbpool.runQuery("SELECT address FROM addresses WHERE username=? AND active=TRUE ORDER BY id LIMIT 1").addCallback(
            _cb)

    @exportRpc("withdraw")
    @limit
    def withdraw(self, currency, address, amount):
        """
        Makes a note in the database that a withdrawal needs to be processed
        :param currency: the currency to process the withdrawal in
        :param address: the address to which the withdrawn money is to be sent
        :param amount: the amount of money to withdraw
        :return: true or false, depending on success
        """
        validate(currency, {"type": "string"})
        validate(address, {"type": "string"})
        validate(amount, {"type": "number"})
        amount = int(amount)

        if amount <= 0:
            return False

        try:
            logging.info('entering withdraw')
            currency = self.session.query(models.Contract).filter_by(ticker='BTC').one()
            print currency
            cancellation = models.Withdrawal(self.user, currency, address, amount)
            print cancellation
            self.session.add(cancellation)
            self.session.commit()
            return True
        except Exception as e:
            self.session.rollback()
            return False


    @exportRpc("get_positions")
    @limit
    def get_positions(self):
        """
        Returns the user's positions
        :return: a dictionary representing the user's positions in various tickers
        """
        return {x.contract.id: {"ticker": x.contract.ticker,
                                "position": x.position,
                                "reference_price": x.reference_price,
                                "denominator": x.contract.denominator,
                                "contract_type": x.contract.contract_type,
                                "inverse_quotes": x.contract.inverse_quotes}
                for x in self.session.query(models.Position).filter_by(user=self.user)}

    @exportRpc("get_profile")
    @limit
    def get_profile(self):
        return {'nickname': self.user.nickname,
                'email': self.user.email
        }

    @exportRpc("change_profile")
    @limit
    def change_profile(self, new_nickname, new_email):
        """
        Updates a user's nickname and email. Can't change
        the user's login, that is fixed.
        """
        try:
            self.user.nickname = new_nickname
            self.user.email = new_email
            self.session.add(self.user)
            self.session.commit()
            return True
        except Exception as e:
            self.session.rollback()
            return False

    @exportRpc("change_password")
    @limit
    def change_password(self, old_password_hash, new_password_hash):
        """
        Changes a users password.  Leaves salt and two factor untouched.
        :param old_password_hash: current password
        :param new_password_hash: new password
        """

        # sanitize
        validate(old_password_hash, {"type": "string"})
        validate(new_password_hash, {"type": "string"})

        if old_password_hash == self.user.password_hash:
            try:
                self.user.password_hash = new_password_hash
                self.session.add(self.user)
                self.session.commit()

                return True
            except Exception as e:
                self.session.rollback()
                return False
        else:
            return False

    @exportRpc("make_account")
    @limit
    def make_account(self, name, password, salt, email):
        """
        creates a new user account based on a name and a password_hash
        :param name: login, username of the user
        :param password: hash of the password
        :param email: email address for the user

        """

        # sanitize
        validate(name, {"type": "string"})
        validate(password, {"type": "string"})
        validate(salt, {"type": "string"})
        validate(email, {"type": "string"})

        try:
            existing = self.session.query(models.User).filter_by(
                username=name).first()
            if existing is not None:
                raise Exception('duplicate')

            user = models.User(name, salt + ":" + password, email)
            self.session.add(user)

            # Set all cash contracts positions to zero
            cash_contracts = self.session.query(models.Contract).filter_by(contract_type='cash').all()
            for contract in cash_contracts:
                cash_pos = models.Position(user, contract)
                cash_pos.reference_price = 0
                self.session.add(cash_pos)

            new_address = self.session.query(models.Addresses).filter_by(
                active=False, user=None).first()
            new_address.active = True
            new_address.user = user
            self.session.merge(new_address)

            self.session.commit()
            return True

        except Exception as e:
            print e
            self.session.rollback()
            return False


    @exportRpc("list_markets")
    @limit
    def list_markets(self):
        """
        Lists markets available for trading
        :return: a list of markets...
        """

        result = {}
        for c in self.session.query(models.Contract).filter_by(active=True):
            # .filter(models.Contract.contract_type != 'cash'):  let's include cash contracts

            result[c.ticker] = {"description": c.description,
                                "denominator": c.denominator,
                                "contract_type": c.contract_type,
                                "full_description": c.full_description,
                                "tick_size": c.tick_size,
                                "lot_size": c.lot_size}

            if c.contract_type == 'futures':
                result[c.ticker]['margin_high'] = c.margin_high
                result[c.ticker]['margin_low'] = c.margin_low

            if c.contract_type == 'prediction':
                result[c.ticker]['final_payoff'] = c.denominator

        return result

    @exportRpc("get_chat_history")
    @limit
    def get_chat_history(self):
        """
        rpc use to load the last n lines of the chat box
        :param ticker: ticker of the book we want
        :return: the book
        """
        # rpc call:
        lastThirty = []

        with open(config.get("webserver", "chat_log")) as f:
            for line in f.read().split('\n')[-31:-1]:
                #strip the date and time from the line:
                lastThirty.append(line.split()[2])

        return lastThirty


    @exportRpc("get_order_book")
    @limit
    def get_order_book(self, ticker):
        """
        rpc used to get the cached order book
        :param ticker: ticker of the book we want
        :return: the book
        """
        # sanitize inputs:
        validate(ticker, {"type": "string"})

        # rpc call:
        if ticker in self.factory.all_books:
            return self.factory.all_books[ticker]
        else:
            return []

    @exportRpc("get_open_orders")
    @limit
    def get_open_orders(self):
        """
        gets open orders
        """
        return [
            {'ticker': order.contract.ticker,
             'price': order.price,
             'quantity': order.quantity_left,
             'side': order.side,
             'id': order.id}
            for order in self.session.query(models.Order).filter_by(
                user=self.user).filter(models.Order.quantity_left > 0) if not order.is_cancelled and order.accepted]


    @exportRpc("place_order")
    @limit
    def place_order(self, order):
        """
        Places an order on the engine
        :param order: the order to be placed
        :return: the order id, some error?
        :raise: some exception? need to do better error checking
        """
        # sanitize inputs:
        validate(order,
                 {"type": "object", "properties": {
                     "ticker": {"type": "string"},
                     "price": {"type": "number"},
                     "quantity": {"type": "number"},
                     "side": {"type": "number"}
                 }})

        # enforce minimum tick_size for prices:
        contract = self.session.query(models.Contract).filter_by(
            ticker=order["ticker"]).order_by(
            models.Contract.id.desc()).first()
        # TODO: solve this another way, i.e. let the user know
        if contract == None:
            raise Exception("Invalid contract ticker.")
        tick_size = contract.tick_size
        lot_size = contract.lot_size

        # coerce tick size and lot size
        order["price"] = int(int(order["price"] / tick_size) * tick_size)
        order["quantity"] = int(int(order["quantity"] / lot_size) * lot_size)
        order['username'] = self.user.username

        self.accountant.push(json.dumps({'place_order': order}))
        self.count += 1
        print 'place_order', self.count

    @exportRpc("get_safe_prices")
    @limit
    def get_safe_prices(self, array_of_tickers):
        validate(array_of_tickers, {"type": "array", "items": {"type": "string"}})
        if array_of_tickers:
            return {ticker: self.factory.safe_prices[ticker] for ticker in array_of_tickers}
        return self.factory.safe_prices

    @exportRpc("cancel_order")
    @limit
    def cancel_order(self, order_id):
        """
        Cancels a specific order
        :param order_id: order_id of the order
        """
        # sanitize inputs:
        print 'received order_id', order_id
        validate(order_id, {"type": "number"})
        print 'received order_id', order_id
        order_id = int(order_id)
        print 'formatted order_id', order_id
        print 'output from server', str({'cancel_order': {'id': order_id, 'username': self.user.username}})
        self.accountant.push(json.dumps({'cancel_order': {'id': order_id, 'username': self.user.username}}))
        self.count += 1
        print 'cancel_order', self.count


    @exportSub("chat")
    def subscribe(self, topicUriPrefix, topicUriSuffix):
        """
        Custom topic subscription handler
        :param topicUriPrefix: prefix of the URI
        :param topicUriSuffix:suffix part, in this case always "chat"
        """
        logging.info("client wants to subscribe to %s%s" % (topicUriPrefix, topicUriSuffix))
        if self.user:
            logging.info("he's logged in as %s so we'll let him" % self.user.username)
            return True
        else:
            logging.info("but he's not logged in, so we won't let him")
            return False

    @exportPub("chat")
    def publish(self, topicUriPrefix, topicUriSuffix, event):
        """
        Custom topic publication handler
        :param topicUriPrefix: prefix of the URI
        :param topicUriSuffix: suffix part, in this case always "general"
        :param event: event being published, a json object
        """
        print 'string?', event
        logging.info("client wants to publish to %s%s" % (topicUriPrefix, topicUriSuffix))
        if not self.user:
            logging.info("he's not logged in though, so no")
            return None
        else:
            logging.info("he's logged as %s in so that's cool" % self.user.username)
            if type(event) not in [str, unicode]:
                logging.warning("but the event type isn't a string, that's way uncool so no")
                return None
            elif len(event) > 0:
                message = cgi.escape(event)
                if len(message) > 128:
                    message = message[:128] + u"[\u2026]"
                chat_log.info('%s:%s' % (self.user.nickname, message))

                #pause message rate if necessary
                time_span = time.time() - self.troll_throttle
                print time_span
                if time_span < 3:
                    time.sleep(time_span)
                    print 'sleeping'
                self.troll_throttle = time.time()
                print self.troll_throttle

                return [cgi.escape(self.user.nickname), message]


class PepsiColaServerFactory(WampServerFactory):
    """
    Simple broadcast server broadcasting any message it receives to all
    currently connected clients.
    """

    def __init__(self, url, debugWamp=False, debugCodePaths=False):
        WampServerFactory.__init__(self, url, debugWamp=debugWamp, debugCodePaths=debugCodePaths)
        self.all_books = {}
        self.safe_prices = {}
        self.cookies = {}
        endpoint = ZmqEndpoint("bind", config.get("webserver", "zmq_address"))
        self.receiver = ZmqPullConnection(zf, endpoint)
        self.receiver.onPull = self.dispatcher

        endpoint = ZmqEndpoint("connect", config.get("accountant", "zmq_address"))
        self.accountant = ZmqPushConnection(zf, endpoint)

    def dispatcher(self, message):
        """
        Dispatches a message on the "simple" pub/sub channel... here for example purposes
        :rtype : NoneType
        :param message: message do be sent
        """

        # TODO: check if message is multipart
        for key, value in json.loads(message[0]).iteritems():
            logging.info("key, value pair for event: %s, %s", json.dumps(key), json.dumps(value))
            if key == 'book_update':
                self.all_books.update(value)
                print "wss://sputnikmkt.com:8000/order_book#%s" % value.keys()[0]
                self.dispatch("wss://sputnikmkt.com:8000/order_book#%s" % value.keys()[0], json.dumps(value))
                #logging.info("Sent:    %", message)

            elif key == 'safe_price':
                self.safe_prices.update(value)
                self.dispatch("wss://sputnikmkt.com:8000/safe_prices#%s" % value.keys()[0], value.values()[0])

            elif key == 'trade':
                self.dispatch("wss://sputnikmkt.com:8000/trades#%s" % value['ticker'], value)
                print 'search'
                print value

            elif key == 'fill':
                self.dispatch("wss://sputnikmkt.com:8000/user/fills#%s" % value[0], value[1])
                print "wss://sputnikmkt.com:8000/user/fills#%s" % value[0], value[1]

            elif key == 'cancel':
                self.dispatch("wss://sputnikmkt.com:8000/user/cancels#%s" % value[0], value[1])
                print "wss://sputnikmkt.com:8000/user/cancels#%s" % value[0], value[1]

            elif key == 'open_orders':
                '''
                note: this should be a private per user channel
                '''
                self.dispatch("wss://sputnikmkt.com:8000/user/open_orders#%s" % value[0], value[1])
                print "wss://sputnikmkt.com:8000/user/open_orders#%s" % value[0], value[1]


class ChainedOpenSSLContextFactory(ssl.DefaultOpenSSLContextFactory):
    def __init__(self, privateKeyFileName, certificateChainFileName,
                 sslmethod=SSL.SSLv23_METHOD):
        """
        @param privateKeyFileName: Name of a file containing a private key
        @param certificateChainFileName: Name of a file containing a certificate chain
        @param sslmethod: The SSL method to use
        """
        self.privateKeyFileName = privateKeyFileName
        self.certificateChainFileName = certificateChainFileName
        self.sslmethod = sslmethod
        self.cacheContext()

    def cacheContext(self):
        ctx = SSL.Context(self.sslmethod)
        ctx.use_certificate_chain_file(self.certificateChainFileName)
        ctx.use_privatekey_file(self.privateKeyFileName)
        self._context = ctx


if __name__ == '__main__':

    logging.basicConfig(level=logging.DEBUG)
    chat_log = logging.getLogger('chat_log')

    chat_log_handler = logging.FileHandler(filename=config.get("webserver", "chat_log"))
    chat_log_formatter = logging.Formatter('%(asctime)s %(message)s')
    chat_log_handler.setFormatter(chat_log_formatter)
    chat_log.addHandler(chat_log_handler)

    if config.getboolean("webserver", "debug"):
        log.startLogging(sys.stdout)
        debug = True
    else:
        debug = False

    # IP address to listen on for all publicly visible services
    interface = config.get("webserver", "interface")

    uri = "ws://"
    contextFactory = None
    if config.getboolean("webserver", "ssl"):
        uri = "wss://"
        key = config.get("webserver", "ssl_key")
        cert = config.get("webserver", "ssl_cert")
        cert_chain = config.get("webserver", "ssl_cert_chain")
        # contextFactory = ssl.DefaultOpenSSLContextFactory(key, cert)
        contextFactory = ChainedOpenSSLContextFactory(key, cert_chain)

    address = config.get("webserver", "ws_address")
    port = config.getint("webserver", "ws_port")
    uri += "%s:%s/" % (address, port)

    factory = PepsiColaServerFactory(uri, debugWamp=debug, debugCodePaths=debug)
    factory.protocol = PepsiColaServerProtocol

    # prevent excessively large messages
    # https://autobahn.ws/python/reference
    factory.setProtocolOptions(maxMessagePayloadSize=1000)

    listenWS(factory, contextFactory, interface=interface)

    if config.getboolean("webserver", "www"):
        web_dir = File(config.get("webserver", "www_root"))
        web = Site(web_dir)
        port = config.getint("webserver", "www_port")
        if config.getboolean("webserver", "ssl"):
            reactor.listenSSL(port, web, contextFactory, interface=interface)
        else:
            reactor.listenTCP(port, web, interface=interface)

    reactor.run()

