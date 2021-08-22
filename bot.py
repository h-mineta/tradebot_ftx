#!/usr/bin/python3
# -*- coding: utf-8 -*-

# Copyright (c) MINETA "m10i" Hiroki <h-mineta@0nyx.net>
#
# pip3 install --user websockets aiohttp jinja2 aiohttp-jinja2 pytz python-dateutil rainbow_logging_handler

import argparse
import asyncio
import hashlib
import hmac
import json
import logging
import os
import sys
import time
import urllib
from datetime import datetime

import aiohttp
import aiohttp_jinja2
import jinja2
import websockets
from pytz import timezone
from rainbow_logging_handler import RainbowLoggingHandler

parser = argparse.ArgumentParser()

parser.add_argument('--rest-url',
                    action='store',
                    nargs='?',
                    default='https://ftx.com',
                    type=str,
                    help='REST API URL')

parser.add_argument('--ws-uri',
                    action='store',
                    nargs='?',
                    default='wss://ftx.com/ws/',
                    type=str,
                    help='WebSocket URI')

parser.add_argument('-l', '--listen-address',
                    action='store',
                    nargs='?',
                    default='0.0.0.0',
                    type=str,
                    help='Listen address(default: 0.0.0.0)')

parser.add_argument('-p', '--listen-port',
                    action='store',
                    nargs='?',
                    default=8080,
                    type=int,
                    help='Listen port number(default: 8080)')

parser.add_argument('--timezone',
                    action='store',
                    nargs='?',
                    default='Asia/Tokyo',
                    type=str,
                    help='Timezone (default: Asia/Tokyo)')

parser.add_argument('--sub-account-name',
                    action='store',
                    nargs='?',
                    default=None,
                    type=str,
                    help='If use sub account (default: None)')

parser.add_argument('--start-enable',
                    action='store_true',
                    default=False)

parser.add_argument('--pips',
                    action='store',
                    nargs='?',
                    default=0.0001,
                    type=float)

parser.add_argument('--order-pips',
                    action='store',
                    nargs='?',
                    default=1,
                    type=int)

parser.add_argument('--order-size',
                    action='store',
                    nargs='?',
                    default=0.01,
                    type=float)

parser.add_argument('--order-repeat-max',
                    action='store',
                    nargs='?',
                    default=0,
                    type=int)

parser.add_argument('--order-repeat-interval',
                    action='store',
                    nargs='?',
                    default=sys.maxsize,
                    type=int)

parser.add_argument('--take-profit-pips',
                    action='store',
                    nargs='?',
                    default=1,
                    type=int)

parser.add_argument('--stop-loss-pips',
                    action='store',
                    nargs='?',
                    default=150,
                    type=int)

parser.add_argument('--max-value-pips',
                    action='store',
                    nargs='?',
                    default=20,
                    type=int)

parser.add_argument('api_key',
                    action='store',
                    nargs='?',
                    default=None,
                    type=str,
                    help='API Key')

parser.add_argument('api_secret',
                    action='store',
                    nargs='?',
                    default=None,
                    type=str,
                    help='API Secret')

parser.add_argument('market',
                    action='store',
                    nargs='?',
                    default=None,
                    type=str,
                    help='Market Name')

parser.add_argument('base_price',
                    action='store',
                    nargs='?',
                    default=None,
                    type=float,
                    help='Base price')

args: dict = parser.parse_args()

handler = RainbowLoggingHandler(sys.stdout)
logging.basicConfig(
    level=logging.DEBUG,
    datefmt='%H:%M:%S',
    format='%(asctime)s[%(levelname)-8s] - %(module)-12s:%(funcName)s:L%(lineno)4d : %(message)s',
    handlers=[handler])
logger = logging.getLogger(__name__)
logging.getLogger("aiohttp").setLevel(logging.WARNING)
logging.getLogger("asyncio").setLevel(logging.WARNING)
logging.getLogger("websockets").setLevel(logging.WARNING)

wallets: list = None
orders: list = []
triggers: list = []
shared_params: dict = {
    "enabled": args.start_enable,
    "base_price": args.base_price,
    "pips": args.pips,
    "order_pips": args.order_pips,
    "order_size": args.order_size,
    "take_profit_pips": args.take_profit_pips,
    "stop_loss_pips": args.stop_loss_pips,
    "max_value_pips": args.max_value_pips,
    "order_repeat_max": args.order_repeat_max,
    "order_repeat_interval": args.order_repeat_interval,

    "timestamp": None,
    "bid": None,
    "sell_lasttime": 0,
    "ask": None,
    "buy_lasttime": 0,
    "market": args.market,
    "not_enough_balances" : {
        "buy" : False,
        "sell" : False
    }
}

def main(args: dict):
    # aiohttp Web server
    app = aiohttp.web.Application()
    aiohttp_jinja2.setup(
        app, loader=jinja2.FileSystemLoader(os.path.join(os.getcwd(), "views"))
    )
    app.add_routes([aiohttp.web.route("*", "/", index)])

    loop = asyncio.get_event_loop()
    try:
        asyncio.ensure_future(trade_client(args))
        aiohttp.web.run_app(app, host=args.listen_address, port=args.listen_port)
    except KeyboardInterrupt:
        pass
    except Exception as ex:
        logger.error(ex)
    finally:
        loop.close()

async def trade_client(args: dict) -> None:
    global shared_params

    timezone_local: str = timezone(args.timezone)

    while True:
        try:
            await get_balances(retry_wait=10)

            await get_open_orders(args.market, retry_wait=10)

            await get_open_trigger_orders(args.market, retry_wait=10)

            async with websockets.connect(args.ws_uri) as websocket:
                await ws_auth(websocket, args.api_key, args.api_secret)
                await ws_subscribe(websocket, ["ticker"], args.market)
                await ws_subscribe(websocket, ["fills", "orders"])

                async for response in websocket:
                    payload = json.loads(response)
                    await trade_logic(args, timezone_local, payload)

        except websockets.exceptions.ConnectionClosed as ex:
            logger.error(ex)
            await asyncio.sleep(10)
            continue
        except KeyboardInterrupt:
            break
        except Exception as ex:
            logger.error(ex)
            await asyncio.sleep(60)
            continue

async def trade_logic(args: dict, timezone_local: str, payload: dict) -> None:
    global shared_params
    global orders

    if "channel" not in payload:
        logger.warning(payload)
        return

    # channel: trades
    if payload["channel"] == "ticker" and "data" in payload:
        timestamp: datetime = datetime.fromtimestamp(int(payload["data"]["time"]))
        timestamp_local: datetime = timestamp.astimezone(timezone_local)

        ask: float = payload["data"]["ask"]
        bid: float = payload["data"]["bid"]

        shared_params["timestamp"] = str(timestamp_local)

        if shared_params["ask"] != ask:
            shared_params["sell_lasttime"] = int(timestamp_local.timestamp())
        shared_params["ask"] = ask

        if shared_params["bid"] != bid:
            shared_params["buy_lasttime"] = int(timestamp_local.timestamp())
        shared_params["bid"] = bid

        if shared_params["enabled"] == True:
            if shared_params["not_enough_balances"]["buy"] == False \
                and bid <= shared_params["base_price"] \
                and bid >= (shared_params["base_price"] - (shared_params["pips"] * shared_params["max_value_pips"])):

                order_price = shared_params["bid"] - (shared_params["pips"] * shared_params["order_pips"])

                if is_ordered(shared_params["market"], "buy", order_price, shared_params["order_repeat_max"], shared_params["order_repeat_interval"]) == False:
                    response = await place_order(
                        shared_params["market"],
                        "buy",
                        order_price,
                        "limit",
                        shared_params["order_size"]
                    )

                    if response["success"] == True:
                        await get_open_orders(args.market)
                    else:
                        logger.warning(response)
                        if "error" in response and response["error"] in ["Not enough balances", "Account does not have enough margin for order."]:
                            shared_params["not_enough_balances"]["buy"] = True

            if shared_params["not_enough_balances"]["sell"] == False \
                and ask >= shared_params["base_price"] \
                and ask < (shared_params["base_price"] + (shared_params["pips"] * shared_params["max_value_pips"])):

                order_price = shared_params["ask"] + (shared_params["pips"] * shared_params["order_pips"])

                if is_ordered(shared_params["market"], "sell", order_price, shared_params["order_repeat_max"], shared_params["order_repeat_interval"]) == False:
                    response = await place_order(
                        shared_params["market"],
                        "sell",
                        order_price,
                        "limit",
                        shared_params["order_size"]
                    )

                    if response["success"] == True:
                        await get_open_orders(args.market)
                    else:
                        logger.warning(response)
                        if "error" in response and response["error"] in ["Not enough balances", "Account does not have enough margin for order."]:
                            shared_params["not_enough_balances"]["sell"] = True

    elif payload["channel"] == "orders" and "data" in payload:
        logger.info(payload)
        await get_open_orders(args.market)
        await get_open_trigger_orders(args.market)

    elif payload["channel"] == "fills" and "data" in payload:
        logger.info(payload)
        shared_params["not_enough_balances"]["buy"] = False
        shared_params["not_enough_balances"]["sell"] = False

    else:
        logger.debug(payload)

    return

async def ws_subscribe(ws: websockets, channels: list, market: str = None) -> bool:
    for channel in channels:
        params = {
            "op" : "subscribe",
            "channel" : channel
        }
        if market is not None:
            params["market"] = market

        await ws.send(json.dumps(params))

async def ws_auth(ws: websockets, api_key: str, api_secret: str) -> bool:
    try:
        milliseconds: int = int(time.time() * 1000)
        signature_payload = "{:s}{:s}".format(str(milliseconds), 'websocket_login')
        signature = hmac.new(api_secret.encode(), signature_payload.encode(), hashlib.sha256).hexdigest()
        params = {
            "op" : "login",
            "args" : {
                "key"  : api_key,
                "sign" : signature,
                "time" : milliseconds,
            }
        }

        await ws.send(json.dumps(params))

        return True
    except Exception as ex:
        raise

async def get_balances(is_retry: bool = True, retry_wait: int = 10) -> None:
    global wallets

    while True:
        response = await request_restapi("GET", "/api/wallet/balances")
        if "success" in response and response["success"] == True:
            wallets = response["result"]
            break
        else:
            logger.error("get balances failed")
            logger.error(response)
            if is_retry == False:
                break

            logger.warning("sleep {:d} sec...".format(retry_wait))
            await asyncio.sleep(retry_wait)

async def get_open_orders(market: str, is_retry: bool = True, retry_wait: int = 10) -> None:
    global orders

    while True:
        response = await request_restapi("GET", "/api/orders?market={:}".format(market))
        if "success" in response and response["success"] == True:
            orders = response["result"]
            break
        else:
            logger.error("get orders failed")
            logger.error(response)
            if is_retry == False:
                break

            logger.warning("sleep {:d} sec...".format(retry_wait))
            await asyncio.sleep(retry_wait)

async def get_open_trigger_orders(market: str, is_retry: bool = True, retry_wait: int = 10) -> None:
    global triggers

    while True:
        response = await request_restapi("GET", "/api/conditional_orders?market={:}".format(market))
        if "success" in response and response["success"] == True:
            triggers = response["result"]
            break
        else:
            logger.error("get trigger orders failed")
            logger.error(response)
            if is_retry == False:
                break

            logger.warning("sleep {:d} sec...".format(retry_wait))
            await asyncio.sleep(retry_wait)

def is_ordered(market: str, side: str, price: float, order_max: int = 0, interval: int = sys.maxsize) -> bool:
    global orders
    global shared_params

    order_count: int = 0

    for order in orders:
        if order["market"] == market and order["side"] == side and order["price"] == price:
            order_count += 1

    if order_count == 0:
        return False
    elif order_count > order_max:
        return True

    now = int(time.time())
    if (now - shared_params["{:s}_lasttime".format(side)]) < interval:
        return True
    else:
        shared_params["{:s}_lasttime".format(side)] = now
        return False

async def place_order(market: str, side: str, price: float, type: str = "limit", size: float = 0.01, reduce_only: bool = False, ioc: bool = False, post_only: bool = False, client_id: str = None) -> dict:
    side = side.lower()
    type = type.lower()

    if side not in ["buy", "sell"]:
        return None
    if type not in ["limit", "market"]:
        return None

    payload = {
        "market": market,
        "side": side,
        "price": price,
        "type": type,
        "size": size,
        "reduceOnly": reduce_only,
        "ioc": ioc,
        "postOnly": post_only,
        "clientId": client_id
    }

    response_order = await request_restapi("POST", "/api/orders", payload)

    return response_order

async def place_trigger_order(market: str, side: str, trigger_price: float, order_price: float = None, type: str = "stop", size: float = 0.01, reduce_only: bool = False, retry_until_filled: bool = False) -> dict:
    side = side.lower()

    if side not in ["buy", "sell"]:
        return None
    if type not in ["stop", "takeProfit"]:
        return None

    payload = {
        "market": market,
        "side": side,
        "type": type,
        "size": size,
        "reduceOnly": reduce_only,
        "retryUntilFilled": retry_until_filled,

        "triggerPrice": trigger_price
    }

    if order_price is not None:
        payload["orderPrice"] = order_price

    response_trigger = await request_restapi("POST", "/api/conditional_orders", payload)

    return response_trigger

async def request_restapi(method: str, path_url: str, payload: dict = None) -> dict:
    method = method.upper()

    try:
        milliseconds: int = int(time.time() * 1000)
        signature_payload = "{ts}{method}{path_url}".format(ts=str(milliseconds), method=method, path_url=path_url)
        if payload is not None:
            signature_payload += json.dumps(payload)
        signature = hmac.new(args.api_secret.encode(), signature_payload.encode(), hashlib.sha256).hexdigest()

        headers = {
            "FTX-KEY": args.api_key,
            "FTX-SIGN": signature,
            "FTX-TS": str(milliseconds)
        }

        if args.sub_account_name is not None:
            headers["FTX-SUBACCOUNT"] = urllib.parse.quote(args.sub_account_name)

        async with aiohttp.ClientSession() as session:
            if method == "GET":
                async with session.get(args.rest_url + path_url, headers=headers) as resp:
                    return await resp.json()
            elif method == "POST":
                async with session.post(args.rest_url + path_url, headers=headers, json=payload) as resp:
                    return await resp.json()
            elif method == "DELETE":
                async with session.delete(args.rest_url + path_url, headers=headers) as resp:
                    return await resp.json()

    except Exception as ex:
        raise
    return None

async def index(request: aiohttp.web.Request) -> aiohttp.web.Response:
    global shared_params
    global wallets
    global orders
    global triggers

    request_data: dict = None

    if request.method == "POST":
        request_data = await request.post()

        if "enable" in request_data:
            if request_data["enable"] in ["1", "true"]:
                shared_params["enabled"] = True
                logger.info("Enabled")
            else:
                shared_params["enabled"] = False
                logger.info("Disabled")
                # reset
                shared_params["not_enough_balances"]["sell"] = False
                shared_params["not_enough_balances"]["buy"] = False

        if "base_price" in request_data:
            try:
                shared_params["base_price"] = float(request_data["base_price"])
            except Exception:
                pass

        if "order_pips" in request_data:
            try:
                shared_params["order_pips"] = int(request_data["order_pips"])
            except Exception:
                pass

        if "order_size" in request_data:
            try:
                shared_params["order_size"] = float(request_data["order_size"])
            except Exception:
                pass

        if "take_profit_pips" in request_data:
            try:
                shared_params["take_profit_pips"] = int(request_data["take_profit_pips"])
            except Exception:
                pass

        if "stop_loss_pips" in request_data:
            try:
                shared_params["stop_loss_pips"] = int(request_data["stop_loss_pips"])
            except Exception:
                pass

        if "max_value_pips" in request_data:
            try:
                shared_params["max_value_pips"] = int(request_data["max_value_pips"])
            except Exception:
                pass

        if "order_repeat_max" in request_data:
            try:
                shared_params["order_repeat_max"] = int(request_data["order_repeat_max"])
            except Exception:
                pass

        if "order_repeat_interval" in request_data:
            try:
                shared_params["order_repeat_interval"] = int(request_data["order_repeat_interval"])
            except Exception:
                pass

        if "cancel_order_id" in request_data:
            try:
                order_id = int(request_data["cancel_order_id"])
                response = await request_restapi("DELETE", "/api/orders/{:d}".format(order_id))
                if "success" in response and response["success"] == True:
                    logger.info("{:}, ID: {:d}".format(response["result"], order_id))
                else:
                    logger.warning(response)

            except Exception:
                pass

    # wallet
    await get_balances(is_retry=False)

    # orders
    await get_open_orders(args.market, is_retry=False)

    # triggers
    await get_open_trigger_orders(args.market, is_retry=False)

    context = {
        "params": shared_params,
        "wallets": wallets,
        "orders": orders,
        "triggers": triggers
    }

    render = aiohttp_jinja2.render_template("index.html", request, context=context)
    return render

if __name__ == '__main__':
    main(args)
