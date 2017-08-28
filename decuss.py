from flask import Flask, render_template, request
from flask_sockets import Sockets
from geventwebsocket.exceptions import WebSocketError
import json
from pubsub import pub
import _thread
import time
from uuid import uuid4

class BadMessage(Exception):
    pass

class CustomFlask(Flask):
    jinja_options = Flask.jinja_options.copy()
    jinja_options.update(dict(
        block_start_string="$(",
        block_end_string=")",
        variable_start_string="${",
        variable_end_string="}",
        comment_start_string="/*",
        comment_end_string="*/",
    ))

app = CustomFlask(__name__)
sockets = Sockets(app)

people = dict()

@sockets.route('/talk')
def talk_socket(ws):
    uuid = str(uuid4())
    state = "new"

    def listen_message(message):
        ws.send(message)
    pub.subscribe(listen_message, "message")

    try:
        while not ws.closed:
            message = ws.receive()
            print("RECEIVED", message)
            recvdata = parse(message)
            senddata = dict(uuid=uuid)

            if state == "new":
                if "action" not in recvdata or recvdata["action"] != "join":
                    raise BadMessage(message)
                if "name" not in recvdata:
                    raise BadMessage(message)

                people[uuid] = recvdata["name"]
                who = dict(action="who", me=uuid, people=people)
                print("SENDING WHO", who)
                ws.send(json.dumps(who))

                senddata.update(action="join", name=recvdata["name"])
                state = "old"
            elif state == "old":
                if "action" not in recvdata:
                    raise BadMessage(message)
                if recvdata["action"] == "rename":
                    if "name" not in recvdata:
                        raise BadMessage(message)
                    people[uuid] = recvdata["name"]
                    senddata.update(action="rename", name=recvdata["name"])
                elif recvdata["action"] == "say":
                    if "message" not in recvdata:
                        raise BadMessage(message)
                    senddata.update(action="say", message=recvdata["message"])
                else:
                    raise BadMessage(message)

            print("SENDING", senddata)
            pub.sendMessage("message", message=json.dumps(senddata))

    except WebSocketError:
        pass
#    except BadMessage as e:
#        print("Bad message: ", e.args)
    finally:
        ws.close()

def parse(message):
    if message:
        recvdata = json.loads(message)
        if type(recvdata) != dict:
            raise BadMessage(message)
        return recvdata
    else:
        raise BadMessage(message)

@app.route("/")
def index():
    return render_template("index.html")

if __name__ == "__main__":
    from gevent import pywsgi
    from geventwebsocket.handler import WebSocketHandler
    server = pywsgi.WSGIServer(('', 5000), app, handler_class=WebSocketHandler)
    server.serve_forever()
