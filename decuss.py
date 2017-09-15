

from flask import (Flask, render_template, request, redirect, url_for, flash,
                   session, g, abort)
from flask_sockets import Sockets
from geventwebsocket.exceptions import WebSocketError
import json
import os
from passlib.hash import argon2
from pubsub import pub
import sqlite3
import sys
try:
    import _thread
except:
    import thread as _thread
import time
from uuid import uuid4

class BadMessage(Exception):
    pass

class Unauthorized(Exception):
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
#TODO:  check if file exists, create if not?
app.secret_key = open("session-secret.txt", "rb").read()

'''
CREATE TABLE users (
    name TEXT PRIMARY KEY,
    uuid TEXT UNIQUE,
    passwordhash TEXT,
    avatarurl TEXT
);
'''

DBFILE = "users.db"

def get_db():
    #TODO check for/generate db file
    db = getattr(g, "_database", None)
    if db is None:
        db = g._database = sqlite3.connect(DBFILE)
        db.row_factory = sqlite3.Row
    return db

@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, "_database", None)
    if db is not None:
        db.close()

def query_db(query, args=(), one=False):
    print(query, args)
    sys.stdout.flush()
    cur = get_db().execute(query, args)
    rv = cur.fetchall()
    cur.close()
    return (rv[0] if rv else None) if one else rv

@app.route("/")
def index():
    return redirect(url_for("chat"))

@app.route("/sign", methods=["GET"])
def signupin():
    return render_template("sign.html")

tokens = dict()

GETPERSON = """
select name,uuid,passwordhash,avatarurl from users where name=:name;
"""

PUTPERSON = """
insert into users (name,uuid,passwordhash,avatarurl) values (:name,:uuid,:passwordhash,:avatarurl);
"""

@app.route("/signed", methods=["POST"])
def dosignupin():
    if request.form["action"] == "Sign in":
        p = query_db(GETPERSON, args=dict(name=request.form["name"]), one=True)
        if p and argon2.verify(request.form["password"], p["passwordhash"]):
            valid = True
            person = dict(name=p["name"],
                          uuid=p["uuid"],
                          avatarurl=p["avatarurl"])
        else:
            valid = False
            invalidmessage = "invalid name or password"
    elif request.form["action"] == "Sign up":
        p = query_db(GETPERSON, args=dict(name=request.form["name"]), one=True)
        if p is not None:
            valid = False
            invalidmessage = "choose a different name"
        else:
            valid = True
            print(request.form)
            sys.stdout.flush()
            person = dict(name=request.form["name"],
                          uuid=str(uuid4()),
                          passwordhash=argon2.hash(request.form["password"]),
                          avatarurl=request.form["avatarurl"])
            query_db(PUTPERSON, args=person)
            get_db().commit()
            del person["passwordhash"]
    else:
        abort(400)

    if valid:
        webtoken = str.join("", ("{:02x}".format(ord(b)) for b in os.urandom(32)))
        tokens[webtoken] = person
        return render_template("signed.html", token=webtoken)
    else:
        flash(invalidmessage)
        return redirect(url_for("signupin"))

@app.route("/chat")
def chat():
    return render_template("chat.html")

@app.route("/authfail")
def authfail():
    flash("please sign in")
    return redirect(url_for("signupin"))

@app.route("/kicked")
def kicked():
    flash("you were kicked")
    return redirect(url_for("signupin"))

@app.route("/zxcvbn.js")
def zxcvbnjs():
    return app.send_static_file("zxcvbn.js")

@app.route("/md5.js")
def md5js():
    return app.send_static_file("md5.js")

peoplehere = dict()

@sockets.route('/chatsocket')
def talk_socket(ws):
    state = "new"

    def listen_message(message):
        ws.send(message)
    pub.subscribe(listen_message, "message")

    try:
        while not ws.closed:
            message = ws.receive()
            print("RECEIVED", message)
            recvdata = parse(message)

            if state == "new":
                if "action" not in recvdata or recvdata["action"] != "join":
                    raise BadMessage(message)

                person = authorize(recvdata)
                if person is None:
                    authfail = dict(action="authfail")
                    print("SENDING AUTHFAIL", authfail)
                    ws.send(json.dumps(authfail))
                    raise Unauthorized(message)

                uuid = person["uuid"]
                peoplehere[uuid] = person["name"]

                you = dict(action="you", assigned_uuid=uuid, name=person["name"])
                print("SENDING YOU", you)
                ws.send(json.dumps(you))

                who = dict(action="who", peoplehere=peoplehere)
                print("SENDING WHO", who)
                ws.send(json.dumps(who))

                senddata = dict(uuid=uuid)
                senddata.update(action="join", name=person["name"])
                state = "old"
            elif state == "old":
                senddata = dict(uuid=uuid)

                if "action" not in recvdata:
                    raise BadMessage(message)
                if recvdata["action"] == "rename":
                    if "name" not in recvdata:
                        raise BadMessage(message)
                    peoplehere[uuid] = recvdata["name"]
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
    except BadMessage as e:
        del peoplehere[uuid]
        if e.args[0] is not None:
            print("Bad message: ", e.args[0])
    finally:
        del listen_message
        ws.close()
        leavedata = dict(uuid=uuid, action="leave")
        print("SENDING", leavedata)
        pub.sendMessage("message", message=json.dumps(leavedata))

def parse(message):
    if message:
        recvdata = json.loads(message)
        if type(recvdata) != dict:
            raise BadMessage(message)
        return recvdata
    else:
        raise BadMessage(message)

def authorize(recvdata):
    if "token" not in recvdata:
        return None

    if recvdata["token"] not in tokens:
        return None

    return tokens[recvdata["token"]] 

'''
@app.route("/.well-known/acme-challenge/zNsfPM8-Zq1spomwPGPq_6k9SwQfizksMp-sFqysAtQ")
def cert_thing():
    return "zNsfPM8-Zq1spomwPGPq_6k9SwQfizksMp-sFqysAtQ.1fR4rMHopGhLr-iWv4EKCc9q8prXaQumtLmWiu0UXP0"
'''

if __name__ == "__main__":
    from gevent import pywsgi
    from geventwebsocket.handler import WebSocketHandler
    server = pywsgi.WSGIServer(('0.0.0.0', 443), app, handler_class=WebSocketHandler, keyfile="domain-key.txt", certfile="domain-crt.txt")
    server.serve_forever()
