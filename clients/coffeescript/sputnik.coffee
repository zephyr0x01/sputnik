if module?
    global.window = require "./window.js"
    global.ab = global.window.ab
    global.EventEmitter = require("./events").EventEmitter

### UI API ###

class @Sputnik extends EventEmitter

    markets: {}

    orders: {}
    positions: {}
    margins: {}
    authenticated: false
    profile:
        email: null
        nickname: null
        audit_secret: null
        audit_hash: null
    chat_messages: []

    constructor: (@uri) ->


        ### Sputnik API  ###

        # network control

    connect: () =>
        ab.connect @uri, @onOpen, @onClose

    close: () =>
        @session?.close()
        @session = null

    # market selection

    follow: (market) =>
        @subscribe "book##{market}", @onBook
        @subscribe "trades##{market}", @onTrade
        @subscribe "safe_prices##{market}", @onSafePrice
        @subscribe "ohlcv##{market}", @onOHLCV

    unfollow: (market) =>
        @unsubscribe "book##{market}"
        @unsubscribe "trades##{market}"
        @unsubscribe "safe_prices##{market}"

    # authentication and account management

    makeAccount: (username, secret, email, nickname) =>
        @log "Computing password hash..."
        salt = Math.random().toString(36).slice(2)
        @authextra =
            salt: salt
            iterations: 1000
        password = ab.deriveKey secret, @authextra

        @call("make_account", username, password, salt, email, nickname).then \
            (result) =>
                @emit "make_account_success", result
            , (error) =>
                @emit "make_account_fail", error

    getProfile: () =>
        @call("get_profile").then (@profile) =>
            @emit "profile", @profile

    changeProfile: (nickname, email) =>
        @call("change_profile", email, nickname).then (@profile) =>
            @updateAuditHash()
            @emit "profile", @profile

    getAudit: () =>
        @call("get_audit").then (wire_audit_details) =>
            audit_details = @copy(wire_audit_details)
            audit_details.timestamp = @dateTimeFormat(audit_details.timestamp)
            for side in [audit_details.liabilities, audit_details.assets]
                for ticker, data of side
                    data.total = @quantityFromWire(ticker, data.total)
                    for position in data.positions
                        position[1] = @quantityFromWire(ticker, position[1])

            @emit "audit_details", audit_details
            @emit "audit_hash", @getAuditHash(wire_audit_details.timestamp)

    getAuditHash: (timestamp) =>
        secret = @profile.audit_secret
        username = @username
        email = @profile.email
        nickname = @profile.nickname
        string = "#{secret}:#{username}:#{nickname}:#{email}:#{timestamp}"
        return CryptoJS.MD5(string).toString(CryptoJS.enc.Base64)

    # TODO: Allow for start and endtimes
    getTransactionHistory: () =>
        @call("get_transaction_history").then (wire_transaction_history) =>
            @log ["Transaction history", wire_transaction_history]
            transaction_history = []
            for transaction in wire_transaction_history
                transaction_history.push @transactionFromWire(transaction)
            @emit "transaction_history", transaction_history

    processHash: () =>
        hash = window.location.hash.substring(1).split('&')
        @log ["Hash", hash]
        args = {}
        for entry in hash
            pair = entry.split('=')
            key = decodeURIComponent(pair[0])
            value = decodeURIComponent(pair[1])
            args[key] = value

        if args.function?
            if args.function == 'change_password_token'
                @username = args.username
                @token = args.token
                @emit args['function'], args

    authenticate: (login, password) =>
        if not @session?
            @wtf "Not connected."

        @session.authreq(login).then \
            (challenge) =>
                @authextra = JSON.parse(challenge).authextra
                secret = ab.deriveKey(password, @authextra)
                signature = @session.authsign(challenge, secret)
                @session.auth(signature).then @onAuthSuccess, @onAuthFail
            , (error) =>
                @onAuthFail error            
                @wtf ["Failed login: Could not authenticate", error]

    changePasswordToken: (new_password) =>
        if not @session?
            @wtf "Not connected."

        @session.authreq(@username).then \
            (challenge) =>
                @authextra = JSON.parse(challenge).authextra
                secret = ab.deriveKey(new_password, @authextra)

                @call("change_password_token", @username, secret, @token).then \
                    (message) =>
                        @log "password change successfully"
                        @emit "change_password_success", message

                        # Reconnect so we can log in
                        @close()
                        @connect()
                    , (error) =>
                        @error "password change error", error
                        @emit "change_password_fail", error

    changePassword: (old_password, new_password) =>
        if not @authenticated
            @wtf "Not logged in."

        old_secret = ab.deriveKey(old_password, @authextra)
        new_secret = ab.deriveKey(new_password, @authextra)
        @call("change_password", old_secret, new_secret).then \
            (message) =>
                @log "password changed successfully"
                @emit "change_password_success", message
            , (error) =>
                @error ["password change error", error]
                @emit "change_password_fail", error

    getResetToken: (username) =>
        @call("get_reset_token", username).then \
            (success) =>
                @emit "get_reset_token_success", success
            , (error) =>
                @emit "get_reset_token_fail", error

    getRequestSupportNonce: (type, success, error) =>
        @call("request_support_nonce", type).then success, error

    restoreSession: (uid) =>
        if not @session?
            @wtf "Not connected."

        @session.authreq(uid).then \
            (challenge) =>
                # TODO: Why is this secret hardcoded?
                secret = "EOcGpbPeYMMpL5hQH/fI5lb4Pn2vePsOddtY5xM+Zxs="
                signature = @session.authsign(challenge, secret)
                @session.auth(signature).then @onAuthSuccess, @onSessionExpired
            , (error) =>
                @wtf "RPC Error: Could not authenticate: #{error}."

    logout: () =>
        @authenticated = false
        @call "logout"
        @close()
        @emit "logout"
        # Reconnect after logout
        @connect()

    getCookie: () =>
        @call("get_cookie").then \
            (uid) =>
                @log "cookie: " + uid
                @emit "cookie", uid

    onAuthSuccess: (permissions) =>
        @log ["authenticated", permissions]
        @authenticated = true

        @getProfile()
        @getSafePrices()
        @getOpenOrders()
        @getPositions()

        @username = permissions.username
        @emit "auth_success", @username

        try
            @subscribe "orders#" + @username, @onOrder
            @subscribe "fills#" + @username, @onFill
            @subscribe "transactions#" + @username, @onTransaction
        catch error
            @log error

    onAuthFail: (error) =>
        @username = null
        [code, reason] = error
        @emit "auth_fail", error

    onSessionExpired: (error) =>
        @emit "session_expired"

    # data conversion

    cstFromTicker: (ticker) =>
        contract = @markets[ticker]
        if contract.contract_type is "cash_pair"
            [t, s] = ticker.split("/")
            source = @markets[s]
            target = @markets[t]
        else
            source = @markets["BTC"]
            target = @markets[ticker]
        return [contract, source, target]

    timeFormat: (timestamp) =>
        dt = new Date(timestamp / 1000)
        return dt.toLocaleTimeString()

    dateTimeFormat: (timestamp) =>
        dt = new Date(timestamp / 1000)
        return dt.toLocaleString()

    dateFormat: (timestamp) =>
        dt = new Date(timestamp / 1000)
        return dt.toLocaleDateString()

    copy: (object) =>
        new_object = {}
        for key of object
            new_object[key] = object[key]
        return new_object

    ohlcvFromWire: (wire_ohlcv) =>
        ticker = wire_ohlcv['contract']
        ohlcv =
            contract: ticker
            open: @priceFromWire(ticker, wire_ohlcv['open'])
            high: @priceFromWire(ticker, wire_ohlcv['high'])
            low: @priceFromWire(ticker, wire_ohlcv['low'])
            close: @priceFromWire(ticker, wire_ohlcv['close'])
            volume: @quantityFromWire(ticker, wire_ohlcv['volume'])
            vwap: @priceFromWire(ticker, wire_ohlcv['vwap'])
            timestamp: @timeFormat(wire_ohlcv['timestamp'])
            period: wire_ohlcv.period
        return ohlcv

    positionFromWire: (wire_position) =>
        ticker = wire_position.contract
        position = @copy(wire_position)
        position.position = @quantityFromWire(ticker, wire_position.position)
        position.reference_price = @priceFromWire(ticker, wire_position.reference_price)
        return position

    orderToWire: (order) =>
        ticker = order.contract
        wire_order = @copy(order)
        wire_order.price = @priceToWire(ticker, order.price)
        wire_order.quantity = @quantityToWire(ticker, order.quantity)
        wire_order.quantity_left = @quantityToWire(ticker, order.quantity_left)
        return wire_order

    orderFromWire: (wire_order) =>
        ticker = wire_order.contract
        order = @copy(wire_order)
        order.price = @priceFromWire(ticker, wire_order.price)
        order.quantity = @quantityFromWire(ticker, wire_order.quantity)
        order.quantity_left = @quantityFromWire(ticker, wire_order.quantity_left)
        order.timestamp = @timeFormat(wire_order.timestamp)
        return order

    bookRowFromWire: (ticker, wire_book_row) =>
        book_row = @copy(wire_book_row)
        book_row.price = @priceFromWire(ticker, wire_book_row.price)
        book_row.quantity = @quantityFromWire(ticker, wire_book_row.quantity)
        return book_row

    tradeFromWire: (wire_trade) =>
        ticker = wire_trade.contract
        trade = @copy(wire_trade)
        trade.price = @priceFromWire(ticker, wire_trade.price)
        trade.quantity = @quantityFromWire(ticker, wire_trade.quantity)
        trade.wire_timestamp = wire_trade.timestamp
        trade.timestamp = @timeFormat(wire_trade.timestamp)
        return trade

    fillFromWire: (wire_fill) =>
        ticker = wire_fill.contract
        fill = @copy(wire_fill)
        fill.fees = @copy(wire_fill.fees)
        fill.price = @priceFromWire(ticker, wire_fill.price)
        fill.quantity = @quantityFromWire(ticker, wire_fill.quantity)
        fill.wire_timestamp = wire_fill.timestamp
        fill.timestamp = @timeFormat(wire_fill.timestamp)
        for fee_ticker, fee of wire_fill.fees
            fill.fees[fee_ticker] = @quantityFromWire(fee_ticker, fee)
        return fill

    transactionFromWire: (wire_transaction) =>
        transaction = @copy(wire_transaction)
        ticker = wire_transaction.contract
        transaction.quantity = @quantityFromWire(ticker, wire_transaction.quantity)
        transaction.timestamp = @timeFormat(wire_transaction.timestamp)
        return transaction

    quantityToWire: (ticker, quantity) =>
        [contract, source, target] = @cstFromTicker(ticker)
        quantity = quantity * target.denominator
        quantity = quantity - quantity % contract.lot_size
        return quantity

    priceToWire: (ticker, price) =>
        [contract, source, target] = @cstFromTicker(ticker)
        price = price * source.denominator * contract.denominator
        price = price - price % contract.tick_size
        return price

    quantityFromWire: (ticker, quantity) =>
        [contract, source, target] = @cstFromTicker(ticker)

        return quantity / target.denominator

    priceFromWire: (ticker, price) =>
        [contract, source, target] = @cstFromTicker(ticker)

        return price / (source.denominator * contract.denominator)

    getPricePrecision: (ticker) =>
        [contract, source, target] = @cstFromTicker(ticker)

        return Math.log(source.denominator / contract.tick_size) / Math.LN10

    getQuantityPrecision: (ticker) =>
        [contract, source, target] = @cstFromTicker(ticker)

        # TODO: account for contract denominator
        return Math.log(target.denominator / contract.lot_size) / Math.LN10

    # order manipulation
    canPlaceOrder: (quantity, price, ticker, side) =>
      new_order =
          quantity: quantity
          quantity_left: quantity
          price: price
          contract: ticker
          side: side
      [low_margin, high_margin] = @calculateMargin @orderToWire new_order
      cash_position = @positions["BTC"].position
      return high_margin <= cash_position.position

    placeOrder: (quantity, price, ticker, side) =>
        order =
            quantity: quantity
            price: price
            contract: ticker
            side: side
        @log ["placing order", order]
        @call("place_order", @orderToWire(order)).then \
            (res) =>
                @emit "place_order_success", res
            , (error) =>
                @emit "place_order_fail", error

    cancelOrder: (id) =>
        @log "cancelling: #{id}"
        @call("cancel_order", id).then \
            (res) =>
                @emit "cancel_order_success", res
            , (error) =>
                @emit "cancel_order_fail", error

    # deposits and withdrawals

    makeCompropagoDeposit: (store, amount, customer_email, send_sms, customer_phone, customer_phone_company) =>
        charge =
          product_price: amount
          payment_type: store
          customer_email: customer_email
          send_sms: send_sms
          customer_phone: customer_phone
          customer_phone_company: customer_phone_company
          currency: "MXN"
        @log ["compropago charge",charge]
        @call("make_compropago_deposit", charge).then \
            (@ticket) =>
                @log ["compropago deposit ticket", ticket]
                @emit "compropago_deposit_success", ticket
            , (error) =>
                @error ["compropago error", error]
                @emit "compropago_deposit_fail", error

    getAddress: (contract) =>
        @call("get_current_address", contract).then \
            (address) =>
                @log "address for #{contract}: #{address}"
                @emit "address", [contract, address]

    newAddress: (contract) =>
        @call("get_new_address", contract).then \
            (address) =>
                @log "new address for #{contract}: #{address}"
                @emit "address", [contract, address]
            , (error) =>
                @error ["new address failure for #{contract}", error]
                @emit "new_address_fail", error

    requestWithdrawal: (ticker, amount, address) =>
        @call("request_withdrawal", ticker, @quantityToWire(ticker, amount), address).then \
        (result) =>
            @log ["request_withdrawal succeeded", result]
            @emit "request_withdrawal_success", result
        , (error) =>
            @error ["request withdrawal fail", error]
            @emit "request_withdrawal_fail", error

    # account/position information
    getSafePrices: () =>
    getOpenOrders: () =>
        @call("get_open_orders").then \
            (@orders) =>
                @log ["orders received", orders]
                orders = {}
                for id, order of @orders
                    if order.quantity_left > 0
                        orders[id] = @orderFromWire(order)

                @emit "orders", orders

    getPositions: () =>
        @call("get_positions").then \
            (@positions) =>
                @log ["positions received", @positions]
                positions = {}
                for ticker, position of @positions
                    positions[ticker] = @positionFromWire(position)

                @emit "positions", positions


    openMarket: (ticker) =>
        @log "Opening market: #{ticker}"

        @emitBook ticker
        @getOrderBook ticker

        @emitTradeHistory ticker
        @getTradeHistory ticker

        @emitOHLCVHistory ticker, "day"
        @getOHLCVHistory ticker, "day"

        @follow ticker

    getOrderBook: (ticker) =>
        @call("get_order_book", ticker).then @onBook

    getTradeHistory: (ticker) =>
        @call("get_trade_history", ticker).then @onTradeHistory

    getOHLCVHistory: (ticker, period) =>
        @call("get_ohlcv_history", ticker, period).then @onOHLCVHistory

    # miscelaneous methods

    chat: (message) =>
        if @authenticated
            @publish "chat", message
            return [true, null]
        else
            return [false, "Not logged in"]

    ### internal methods ###

    # RPC wrapper
    call: (method, params...) =>
        if not @session?
            return @wtf "Not connected."
        @log ["RPC #{method}",params]
        d = ab.Deferred()
        @session.call("#{@uri}/rpc/#{method}", params...).then \
            (result) =>
                if result.length != 2
                    @warn "RPC Warning: sputnik protocol violation in #{method}"
                    return d.resolve result
                if result[0]
                    return d.resolve result[1]
                else
                    @warn ["RPC call failed", result[1]]
                    return d.reject result[1]
            , (error) =>
                @wtf "RPC Error: #{error.desc} in #{method}"


    subscribe: (topic, callback) =>
        if not @session?
            return @wtf "Not connected."
        @log "subscribing: #{topic}"
        @session.subscribe "#{@uri}/feeds/#{topic}", (topic, event) ->
            callback event

    unsubscribe: (topic) =>
        if not @session?
            return @wtf "Not connected."
        @log "unsubscribing: #{topic}"
        @session.unsubscribe "#{@uri}/feeds/#{topic}"

    publish: (topic, message) =>
        if not @session?
            return @wtf "Not connected."
        @log [topic, message]
        @session.publish "#{@uri}/feeds/#{topic}", message, false

    # logging
    log: (obj) =>
        @emit "log", obj
    warn: (obj) ->
        @emit "warn", obj
    error: (obj) ->
        @emit "error", obj
    wtf: (obj) => # What a Terrible Failure
        @error obj
        @emit "wtf", obj

    # connection events
    onOpen: (@session) =>
        @log "Connected to #{@uri}."
        @processHash()

        @call("get_markets").then @onMarkets, @wtf
        @subscribe "chat", @onChat
        # TODO: Are chats private? Do we want them for authenticated users only?
        @call("get_chat_history").then \
            (chats) =>
                for chat in chats
                    user = chat[0]
                    msg = chat[1]
                    @chat_messages.push "#{user}: #{msg}"
                @emit "chat", @chat_messages

        @emit "open"

    onClose: (code, reason, details) =>
        @log "Connection lost."
        @emit "close", [code, reason, details]

    # authentication internals

    # default RPC callbacks

    onMarkets: (@markets) =>
        for ticker of markets
            @markets[ticker].trades = []
            @markets[ticker].bids = []
            @markets[ticker].asks = []
            @markets[ticker].ohlcv = {day: {}, hour: {}, minute: {}}


        @emit "markets", @markets

    # feeds
    onBook: (book) =>
        @log ["book received", book]

        @markets[book.contract].bids = book.bids
        @markets[book.contract].asks = book.asks
        @emitBook book.contract

    emitBook: (ticker) =>
        ui_book = 
            bids: (@bookRowFromWire(ticker, order) for order in @markets[ticker].bids)
            asks: (@bookRowFromWire(ticker, order) for order in @markets[ticker].asks)
            contract: ticker

        ui_book.bids.sort (a, b) -> b.price - a.price
        ui_book.asks.sort (a, b) -> a.price - b.price

        @log ui_book
        @emit "book", ui_book

    # Make sure we only have the last hour of trades
    cleanTradeHistory: (ticker) =>
        now = new Date()
        an_hour_ago = new Date()
        an_hour_ago.setHours(now.getHours() - 1)
        while @markets[ticker].trades[0].timestamp / 1000 < an_hour_ago.getTime()
            @markets[ticker].trades.shift()

    emitTradeHistory: (ticker) =>
        trade_history = {}
        trade_history[ticker] = for trade in @markets[ticker].trades
            @tradeFromWire(trade)

        @emit "trade_history", trade_history

    onTradeHistory: (trade_history) =>
        @log ["trade_history received", trade_history]
        if trade_history.length > 0
            ticker = trade_history[0].contract
            @markets[ticker].trades = trade_history
            @cleanTradeHistory(ticker)
        else
            @warn "no trades in history"

        @emitTradeHistory(ticker)

    onOHLCV: (ohlcv) =>
        @log ["ohlcv", ohlcv]
        period = ohlcv.period
        ticker = ohlcv.contract
        timestamp = ohlcv.timestamp
        @markets[ticker].ohlcv[period][timestamp] = ohlcv

        @emit "ohlcv", @ohlcvFromWire(ohlcv)
        @emitOHLCVHistory(ticker, period)

    onOHLCVHistory: (ohlcv_history) =>
        @log ["ohlcv_history received", ohlcv_history]
        timestamps = Object.keys(ohlcv_history)
        if timestamps.length
            ticker = ohlcv_history[timestamps[0]].contract
            period = ohlcv_history[timestamps[0]].period
            @markets[ticker].ohlcv[period] = ohlcv_history
            @emitOHLCVHistory(ticker, period)
        else
            @warn "ohlcv_history is empty"

    emitOHLCVHistory: (ticker, period) =>
        ohlcv = {}
        for timestamp, entry of @markets[ticker].ohlcv[period]
            ohlcv[timestamp] = @ohlcvFromWire(entry)
        @emit "ohlcv_history", ohlcv

    onTrade: (trade) =>
        ticker = trade.contract
        @markets[ticker].trades.push trade
        @emit "trade", @tradeFromWire(trade)
        @cleanTradeHistory(ticker)
        @emitTradeHistory(ticker)

    onChat: (event) =>
        # TODO: Something is wrong where my own chats don't show up in this box-- but they do get sent
        user = event[0]
        message = event[1]
        @chat_messages.push "#{user}: #{message}"
        @log "Chat: #{user}: #{message}"
        @emit "chat", @chat_messages

    # My orders get updated with orders
    onOrder: (order) =>
        @log ["Order received", order]
        @emit "order", @orderFromWire(order)

        id = order.id
        if id of @orders and (order.is_cancelled or order.quantity_left == 0)
            delete @orders[id]
        else
            if order.quantity_left > 0
                @orders[id] = order

        orders = {}
        for id, order of @orders
            if order.quantity_left > 0
                orders[id] = @orderFromWire(order)

        @emit "orders", orders

    # Fills don't update my cash, transaction feed does
    onFill: (fill) =>
        @log ["Fill received", fill]
        @emit "fill", @fillFromWire(fill)

    onTransaction: (transaction) =>
        @log ["transaction received", transaction]
        @positions[transaction.contract].position += transaction.quantity
        @emit "transaction", @transactionFromWire(transaction)

        positions = {}
        for ticker, position of @positions
            positions[ticker] = @positionFromWire(position)

        @emit "positions", positions

    calculateMargin: (new_order) =>
        low_margin = 0
        high_margin = 0
        #TODO: add futures and contracts here

        # cash positions
        cash_spent = {}
        for ticker of @markets
            # "defaultdict"
            if @markets[ticker].contract_type is "cash"
                cash_spent[ticker] = 0

        orders = (order for id, order of @orders)
        if new_order?
            orders.push new_order
        for order in orders
            if @markets[order.contract].contract_type is "cash_pair"
                [target, source] = order.contract.split("/")
                switch order.side
                    when "BUY"
                        # TODO: make sure to adjust for contract denominator
                        transaction = order.quantity_left * order.price / 1e8
                        cash_spent[source] += transaction
                    when "SELL"
                        cash_spent[target] += order.quantity_left

        additional = 0
        for ticker, spent of cash_spent
            if ticker is "BTC"
                additional += spent
            else
                position = @positions[ticker]?.position or 0
                additional += if position >= spent then 0 else Math.pow(2, 48)

        low_margin += additional
        high_margin += additional

        @log [low_margin, high_margin]
        return [low_margin, high_margin]

if module?
    module.exports =
        Sputnik: @Sputnik
