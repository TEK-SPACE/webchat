#! /usr/bin/env python2.7
'''
The main file for the webchat, this is the backend that glues all the components
toghether
'''

from flask import (
    Flask, render_template, redirect, url_for, session, Response, request, json
)
from forms import ConnectForm, ChatForm
from simplekv.fs import FilesystemStore
from flaskext.kvsession import KVSessionExtension
from jinja2 import utils
import redis
import logging
import os

from event import MessageEvent, ErrorEvent, UsersEvent, PingEvent
import const

path = os.path.abspath(__file__)
dirname = os.path.dirname(path)
log_path = os.path.join(dirname, 'logs.log')
data_path = os.path.join(dirname, 'data')
keys_path = os.path.join(dirname, 'keys')

if not os.path.isdir(data_path):
    os.mkdir(data_path)

app = Flask(__name__)

with open(keys_path, 'r') as f:
    app.secret_key = f.readline()
    app.config['RECAPTCHA_PUBLIC_KEY'] = f.readline()
    app.config['RECAPTCHA_PRIVATE_KEY'] = f.readline()


store = FilesystemStore(data_path)
sess_ext = KVSessionExtension(store, app)

r = redis.Redis()

logging.basicConfig(filename=log_path, level=logging.DEBUG,
                    format='%(levelname)s: %(asctime)s - %(message)s',
                    datefmt='%d-%m-%Y %H:%M:%S')


@app.route('/', methods=['GET', 'POST'])
def index():
    '''
    Handle the login
    '''
    sess_ext.cleanup_sessions()

    if 'nick' in session:
        return redirect(url_for('chat'))

    form = ConnectForm()

    errors = []

    if form.validate_on_submit():
        try:
            if r.sismember('user_list', form.nick.data):
                form.nick.errors.append(const.UsedNickError)
            else:
                r.sadd('user_list', form.nick.data)
                session['nick'] = form.nick.data
                session['rooms'] = []
                rooms = form.rooms.data.split()

                if not rooms:
                    session['rooms'] = ['global']
                else:
                    rooms = list(set(rooms))
                    for room in rooms:
                        session['rooms'].append(room)

                for room in session['rooms']:
                    add_user(form.nick.data, room)

                session.regenerate() # anti session-fixation attack

                try:
                    r.publish('webchat.users', json.dumps(create_user_dict()))
                except redis.RedisError as e:
                    logging.critical(e)
                    session.destroy()
                    errors.append(const.UnexpectedBackendError)
                else:
                    return redirect(url_for('chat'))
        except redis.RedisError as e:
            logging.critical(e)
            errors.append(const.UnexpectedBackendError)

    return render_template('index.html', form=form, errors=errors)


@app.route('/chat', methods=['GET', 'POST'])
def chat():
    '''The chat (as well as the quit) action is handled here
    When a user connects a user list is fetched from redis and displayed
    Also a connection to our server sent events stream is established, too
    '''
    sess_ext.cleanup_sessions()
    form = ChatForm()

    if 'nick' not in session: # user has no nickname (he's not logged in)
        return redirect(url_for('index'))

    if form.quit.data:
        return disconnect()

    users = None
    errors = []
    try:
        users = json.dumps(create_user_dict())
    except redis.RedisError as e:
        logging.critical(e)
        errors.append(const.GetUsersError)

    form.rooms.data = json.dumps(session['rooms'])

    return render_template('chat.html', nick=session['nick'], form=form,
        users=users, errors=errors)


@app.route('/_publish_message', methods=['POST'])
def publish_message():
    '''When a user submits the chat form this route is called via AJAX
    The user's message is then published to redis and sent to every connected
    client via server sent events
    '''
    if 'nick' not in session:
        return Response(const.NotAuthentifiedError, 403)

    try:
        message = request.form['message'].strip()
        room = request.form['room'].strip()

        if message and len(message) >= 1:
            if room in session['rooms']:
                message= str(utils.escape(message).encode('utf-8'))
                message = message.replace('\n', '<br />')

                logging.info("{0} ({1}): {2}".format(
                    session['nick'], room, message))

                r.publish('webchat.room.' + room, json.dumps({
                    'message': message,
                    'nick': session['nick'],
                    'room': room,
                }))
            else:
                return Response(const.WrongRoom, 403)

    except redis.ConnectionError as e:
        logging.critical(e)
        return Response(const.ConnectionError, 500)
    except redis.RedisError as e:
        logging.critical(e)
        return Response(const.UnexpectedBackendError, 500)
    except Exception as e:
        logging.critical(e)
        return Response(const.UnexpectedError, 500)

    return const.OK


@app.route('/_sse_stream')
def sse_stream():
    '''The server sent events are sent from here'''
    if 'nick' not in session:
        return Response(const.NotAuthentifiedError, 403)

    try:
        if 0 == r.scard('user_list'):
            return Response(const.NoUsers, 404)
    except redis.RedisError as e:
        logging.critical(e)

    return Response(get_event(), mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache'})


@app.route('/_join_rooms', methods=['POST'])
def join_rooms():
    '''Join some rooms while the user is logged in'''
    if 'nick' not in session:
        return Response(const.NotAuthentifiedError, 403)

    form = ChatForm()

    form.join_rooms.data = request.form['join_rooms']

    if not form.join_rooms.data:
        return Response('', 400)

    if form.join_rooms.validate(form):
        rooms = form.join_rooms.data.split()
        session['rooms'].extend(rooms)
        session['rooms'] = list(set(session['rooms']))

        try:
            for room in session['rooms']:
                add_user(session['nick'], room)

            r.publish('webchat.users', json.dumps(create_user_dict()))
        except redis.RedisError as e:
            logging.critical(e)

        return Response(json.dumps(session['rooms']), 200)
    else:
        return Response(const.InvalidRoomError, 400)


@app.route('/_leave_room', methods=['POST'])
def leave_room():
    '''Removes a room from the user's session, thus not allowing him to chat
    there anymore

    Note: this is done on request, the users asks to leave, he's not kicked
    '''
    if 'nick' not in session:
        return Response(const.NotAuthentifiedError, 403)

    room = request.form['room']

    if room and len(room) >= 1 and room in session['rooms']:
        session['rooms'].remove(room)

        if not session['rooms']:
            disconnect()
            return Response(status=404)

        try:
            del_user(session['nick'], [room])
            r.publish('webchat.users', json.dumps(create_user_dict()))
        except redis.RedisError as e:
            logging.critical(e)

        return Response(json.dumps(session['rooms']), 200)
    else:
        return Response(const.InvalidRoomError, 400)


def get_event():
    '''Yields an Event object according to what is received via redis on the
    subscribed channels
    '''
    try:
        pubsub = r.pubsub()
        pubsub.psubscribe('webchat.*')
    except redis.RedisError as e:
        logging.critical(e)
        yield ErrorEvent(const.UnexpectedBackendError)
    except Exception as e:
        logging.critical(e)
        yield ErrorEvent(const.UnexpectedError)
    else:
        for event in pubsub.listen():
            if 'pmessage' == event['type']:
                if 'webchat.room.' in event['channel']:
                    yield MessageEvent(event['data'])
                elif 'webchat.users' == event['channel']:
                    yield UsersEvent(event['data'])
                elif 'webchat.ping' == event['channel']:
                    yield PingEvent()


@app.route('/_pong', methods=['POST'])
def pong():
    '''Handle the PONG sent as a response to PING, this way the application is
    aware of the users still connected (those who respond to PING)
    '''
    if 'nick' not in session:
        return Response(const.NotAuthentifiedError, 403)

    try:
        r.sadd('user_list', session['nick'])

        for room in session['rooms']:
            add_user(session['nick'], room)

        r.publish('webchat.users', json.dumps(create_user_dict()))
    except redis.RedisError as e:
        logging.critical(e)
        return Response(const.UnexpectedBackendError, 500)
    else:
        return const.OK


@app.route('/quit', methods=['GET'])
def disconnect():
    '''Logout'''
    try:
        r.srem('user_list', session['nick'])
        del_user(session['nick'], r.hkeys('users'))
        r.publish('webchat.users', json.dumps(create_user_dict()))
    except redis.RedisError as e:
        logging.critical(e)

    session.destroy()
    sess_ext.cleanup_sessions()

    return redirect(url_for('index'))


def add_user(nick, room):
    '''Add a user the the room's user list'''

    current_users = r.hget('users', room)

    if current_users:
        current_users = json.loads(current_users)
        current_users.append(nick)

        current_users = list(set(current_users))
    else:
        current_users = [nick]

    r.hset('users', room, json.dumps(current_users))


def del_user(nick, rooms):
    '''Remove a user from the user list of every room in the `rooms` list'''
    users = r.hgetall('users')

    for room in rooms:
        current_users = json.loads(users[room])

        try:
            current_users.remove(nick)
        except ValueError:
            pass

        if not current_users:
            r.hdel('users', room)
        else:
            r.hset('users', room, json.dumps(current_users))


def create_user_dict():
    '''Return a dictionary of lists
    The keys are the rooms and the values are user lists
    '''
    users = r.hgetall('users')
    users_dict = {}

    for room, user_list in users.iteritems():
        users_dict[room] = json.loads(user_list)

    return users_dict


if __name__ == '__main__':
    sess_ext.cleanup_sessions()

    app.run(debug=True, threaded=True, port=5005)
    #app.run(debug=False, threaded=True, port=5003, host='0.0.0.0')