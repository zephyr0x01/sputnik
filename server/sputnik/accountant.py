#!/usr/bin/env python

import json

__author__ = 'satosushi'

from sqlalchemy.orm.exc import NoResultFound
import zmq
import models
import database as db
import logging

from optparse import OptionParser
parser = OptionParser()
parser.add_option("-c", "--config", dest="filename",
        help="config file", default="../config/sputnik.ini")
(options, args) = parser.parse_args()

from ConfigParser import SafeConfigParser
config = SafeConfigParser()
config.read(options.filename)

context = zmq.Context()
connector = context.socket(zmq.constants.PULL)
connector.bind(config.get("accountant", "zmq_address"))

db_session = db.Session()

logging.basicConfig(level=logging.DEBUG)


# type of messages:

# deposit/withdraw: adds or remove coins from the account
# increase/decrease required_margin: adds or remove to the required margin


btc = db_session.query(models.Contract).filter_by(ticker='BTC').one()


def create_or_get_position(user, contract, ref_price):
    """
    returns the position in the database for a contract or creates it should it not exist
    :param user: the user
    :param contract: the contract
    :param ref_price: which price is the position entered at?
    :return: the position object
    """
    try:
        return db_session.query(models.Position).filter_by(username=user, contract_id=contract).one()
    except NoResultFound:
        user = db_session.query(models.User).filter_by(id=user).one()
        contract = db_session.query(models.Contract).filter_by(id=contract).one()
        pos = models.Position(user, contract)
        pos.reference_price = ref_price
        db_session.add(pos)
        return pos


def calculate_margin(username, order_id=None):
    """
    calculates the low and high margin for a given user
    :param order_id: order we're considering throwing in
    :param username: the username
    :return: low and high margin
    """
    low_margin = high_margin = 0

    # let's start with positions
    positions = {position.contract_id: position for position in
                 db_session.query(models.Position).filter_by(username=username)}

    open_orders = db_session.query(models.Order).filter_by(username=username).filter(
        models.Order.quantity_left > 0).filter_by(is_cancelled=False, accepted=True).all()

    if order_id:
        open_orders += db_session.query(models.Order).filter_by(id=order_id).all()

    for position in positions.values():

        max_position = position.position + sum(
            order.quantity_left for order in open_orders if order.contract == position.contract and order.side == 'BUY')
        min_position = position.position - sum(
            order.quantity_left for order in open_orders if order.contract == position.contract and order.side == 'SELL')

        # if potential_order and position.contract_id == potential_order['contract_id']:
        #     if potential_order['side'] == 0:
        #         max_position += potential_order['quantity']
        #     if potential_order['side'] == 1:
        #         min_position -= potential_order['quantity']

        contract = position.contract

        if contract.contract_type == 'futures':

            SAFE_PRICE = safe_prices[position.contract.ticker]

            logging.info(low_margin)
            print 'max position:', max_position
            print 'contract.margin_low :', contract.margin_low
            print 'SAFE_PRICE :', SAFE_PRICE
            print 'position.reference_price :', position.reference_price
            print position
            low_max = abs(max_position) * contract.margin_low * SAFE_PRICE / 100 + max_position * (
                position.reference_price - SAFE_PRICE)
            low_min = abs(min_position) * contract.margin_low * SAFE_PRICE / 100 + min_position * (
                position.reference_price - SAFE_PRICE)
            high_max = abs(max_position) * contract.margin_high * SAFE_PRICE / 100 + max_position * (
                position.reference_price - SAFE_PRICE)
            high_min = abs(min_position) * contract.margin_high * SAFE_PRICE / 100 + min_position * (
                position.reference_price - SAFE_PRICE)
            logging.info( low_max)
            logging.info( low_min)

            high_margin += max(high_max, high_min)
            low_margin += max(low_max, low_min)

        if contract.contract_type == 'prediction':
            payoff = contract.denominator

            # case where all our buy orders are hit
            max_spent = sum(order.quantity_left * order.price for order in open_orders if
                            order.contract == contract and order.side == 'BUY')

            # case where all out sell orders are hit
            max_received = sum(order.quantity_left * order.price for order in open_orders if
                               order.contract == contract and order.side == 'SELL')

            # if potential_order and position.contract_id == potential_order['contract_id']:
            #     if potential_order['side'] == 0:
            #         max_spent += potential_order['quantity'] * potential_order['price']
            #     if potential_order['side'] == 1:
            #         max_received += potential_order['quantity'] * potential_order['price']

            worst_short_cover = -min_position * payoff if min_position < 0 else 0
            best_short_cover = -max_position * payoff if max_position < 0 else 0

            additional_margin = max(max_spent + best_short_cover, -max_received + worst_short_cover)
            low_margin += additional_margin
            high_margin += additional_margin

    return low_margin, high_margin


#todo replace deleting rejected orders with marking them as rejected, using an enum
def accept_order_if_possible(username, order_id):
    """
    Checks the impact of an order on margin, and if said impact is acceptable, mark the order as accepted
    otherwise delete the order
    :param username: the username
    :param order_id: order we're considering accepting
    :return:
    """
    low_margin, high_margin = calculate_margin(username, order_id)
    cash_position = db_session.query(models.Position).filter_by(username=username, contract=btc).one()

    order = db_session.query(models.Order).get(order_id)
    logging.info(
        "high_margin = %d, low_margin = %d, cash_position = %d" % (high_margin, low_margin, cash_position.position))

    if high_margin > cash_position.position:
        db_session.delete(order)
        db_session.commit()
        return False
    else:
        order.accepted = True
        db_session.merge(order)
        db_session.commit()
        return True

        #todo: make actual margin calls here


def check_and_issue_margin_call(username):
    """
    Check if a naughty user is due for a margin call!
    :param username: username of the potentially naughty user
    """
    low_margin, high_margin = calculate_margin(username)
    cash_position = db_session.query(models.Position).filter_by(contract=btc, username=username).one()

    if cash_position < low_margin:
        #todo panic!
        logging.warning("Here is where code should be to do something user %d's margin" % username)

    elif cash_position < high_margin:
        logging.warning("Here is where code should be to send user %d a margin call" % username)

    else:
        logging.info("user %d's margin is fine and dandy" % username)


def process_trade(trade):
     """
     takes in a trade and updates the database to reflect that the trade happened
     :param trade: the trade
     """
     print trade
     if trade['contract_type'] == 'futures':
         cash_position = db_session.query(models.Position).filter_by(contract=btc, username=trade['username']).one()
         future_position = create_or_get_position(trade['username'], trade['contract'], trade['price'])
 
         #mark to current price as if everything had been entered at that price and profit had been realized
         cash_position.position += (trade['price'] - future_position.reference_price) * future_position.position
         future_position.reference_price = trade['price']
 
         #note that even though we're transferring money to the account, this money may not be withdrawable
         #because the margin will raise depending on the distance of the price to the safe price
 
         # then change the quantity
         future_position.position += trade['signed_qty']
 
         db_session.merge(future_position)
         db_session.merge(cash_position)
 
     elif request_details['contract_type'] == 'prediction':
         cash_position = db_session.query(models.Position).filter_by(contract=btc, username=trade['username']).one()
         prediction_position = create_or_get_position(trade['username'], trade['contract'], 0)
 
         cash_position.position -= trade['signed_qty'] * trade['price']
         prediction_position.position += trade['signed_qty']
 
         db_session.merge(prediction_position)
         db_session.merge(cash_position)
 
     else:
         logging.error("unknown contract type")
 
     db_session.commit()


def cancel_order(details):
    """
    Cancels an order by id
    :param username:
    :param order_id:
    :return:
    """

    print 'accountant received', details
    order_id = details['order_id']
    username = details['username']
    try:
        # sanitize inputs:
        order_id = int(order_id)
        # try db query
        order = db_session.query(models.Order).filter_by(id=order_id).one()
        if order.username != username:
            return False

        m_e_order = order.to_matching_engine_order()
        m_e_order['is_a_cancellation'] = True
        engine_sockets[order.contract_id].send(json.dumps(m_e_order))
        return True

    except NoResultFound:
        return False


def place_order(order):
    """
    Places an order
    :param order: dictionary representing the order to be placed
    :return: id of the order placed or -1 if failure
    """
    try:
        user = db_session.query(models.User).get(order['username'])
        if "contract_id" in order:
            contract = db_session.query(models.Contract).filter_by(id=order['contract_id']).one()
        else:
            contract = session.query(models.Contract).filter_by(
                ticker=order["ticker"]).order_by(
                        models.Contract.id.desc()).first()
        # check that the price is an integer and within a valid range

        # case of predictions
        if contract.contract_type == 'prediction':
            # contract.denominator happens to be the same as the finally payoff
            if not 0 <= order["price"] <= contract.denominator:
                return False

        o = models.Order(user, contract, order["quantity"], order["price"], "BUY" if order["side"] == 0 else "SELL")

        db_session.add(o)
        db_session.commit()

        if accept_order_if_possible(user.id, o.id):
            m_e_order = o.to_matching_engine_order()
            engine_sockets[o.contract_id].send(json.dumps(m_e_order))
        else:
            logging.info("lol you can't place the order, you don't have enough margin")
    except Exception as e:
        db_session.rollback()
        raise e

def deposit_cash(details):
    """
    Deposits cash
    :param address:
    :param total_received:
    :return:
    """
    try:
        print 'received', details
        currency = btc
        address = details['address']
        total_received = details['total_received']

        # sanitize inputs:
        address = str(address)
        total_received = int(total_received)

        #query for db objects we want to update
        total_deposited_at_address = db_session.query(models.Addresses).filter_by(address=address).one()
        user_cash_position = db_session.query(models.Position).filter_by(username=total_deposited_at_address.username,contract=currency).one()

        #prepare cash deposit
        deposit = total_received - total_deposited_at_address.accounted_for
        print 'updating ', user_cash_position, ' to '
        user_cash_position.position += deposit
        print user_cash_position
        print 'with a deposit of: ',deposit

        #prepare record of deposit
        total_deposited_at_address.accounted_for = total_received

        db_session.add(total_deposited_at_address)
        db_session.add(user_cash_position)

        db_session.commit()
        return True

    except NoResultFound:
        db_session.rollback()
        return False

def clear_contract(details):
    try:
        contract = db_session.query(models.Contract).filter_by(
                id=details["id"]).first()
        # disable new orders on contract
        contract.active = False
        # cancel all pending orders
        orders = db_session.query(models.Order).filter_by(
                contract=contracti, is_cancelled=False).all()
        for order in orders:
            cancel_order({"username":order.username, "order_id":order.id})
        # place orders on behalf of users
        positions = db_session.query(models.Position).filter_by(
                contract=contract).all()
        for position in positions:
            order = {}
            order["username"] = position.username
            order["contract_id"] = position.contract_id
            if position.position > 0:
                order["quantity"] = position.position
                order["side"] = 0 # sell
            elif position.position < 0:
                order["quantity"] = -position.position
                order["side"] = 1 # buy
            order["price"] = details["price"]
            place_order(order)
    except:
        db_session.rollback()

engine_sockets = {i.id: context.socket(zmq.constants.PUSH)
                  for i in db_session.query(models.Contract).filter_by(active=True)}

for contract_id, socket in engine_sockets.iteritems():
    socket.connect('tcp://%s:%d' % ("localhost", 4200 + contract_id))

safe_prices = {}
for c in db_session.query(models.Contract):
    # this should be refined at some point for a better
    # initial safe value
    try:
        last_trade = db_session.query(models.Trade).filter_by(contract=c).order_by(
            models.Trade.timestamp.desc()).first()
        #round to an int for safe prices
        safe_prices[c.ticker] = int(last_trade.price)
    except:
        logging.warning("warning, missing last trade for contract: %s. Using 42 as a stupid default" % c.ticker)
        safe_prices[c.ticker] = 42

#TODO: make one zmq socket for each connecting service (webserver, engine, leo)
while True:
    request = connector.recv_json()
    for request_type, request_details in request.iteritems():
        if request_type == 'safe_price':
            safe_prices.update(request_details)
        elif request_type == 'trade':
            process_trade(request_details)
        elif request_type == 'place_order':
            place_order(request_details)
        elif request_type == 'cancel_order':
            cancel_order(request_details)
        elif request_type == 'deposit_cash':
            deposit_cash(request_details)
        elif request_type == 'clear':
            clear_contract(request_details)
        else:
            logging.warning("unknown request type: %s", request_type)

