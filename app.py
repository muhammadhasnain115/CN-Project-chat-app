from flask import Flask, render_template, request, redirect, session, url_for, send_from_directory, jsonify, Response
from flask_socketio import SocketIO, emit, join_room
from datetime import datetime
import os
from werkzeug.utils import secure_filename
import mimetypes

app = Flask(__name__)
app.secret_key = 'supersecretkey'
app.config['UPLOAD_FOLDER'] = 'uploads'
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

# Allowed file extensions
ALLOWED_EXTENSIONS = {'txt', 'pdf', 'png', 'jpg', 'jpeg', 'gif', 'mp4', 'mp3', 'ogg', 'wav'}

socketio = SocketIO(app, async_mode='eventlet')

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

# In-memory storage
users = {}  # username: {password, messages: {sender: [messages]}, files: {sender: [files]}, unread: {sender: count}}
online_users = {}  # username: socket_id
active_chats = {}  # username: current_chat_with

@app.route('/')
def index():
    if 'username' not in session or session['username'] not in users:
        return redirect('/login')
    return render_template('chat.html', 
                         username=session['username'],
                         users=[u for u in users if u != session['username']])

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        if username in users:
            return 'Username exists', 400
        users[username] = {
            'password': password,
            'messages': {},
            'files': {},
            'unread': {}
        }
        session['username'] = username
        socketio.emit('user_update', {'type': 'new', 'username': username})
        return redirect('/')
    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        if username in users and users[username]['password'] == password:
            session['username'] = username
            return redirect('/')
        return 'Invalid credentials', 401
    return render_template('login.html')

@app.route('/logout')
def logout():
    username = session.pop('username', None)
    if username in online_users:
        del online_users[username]
    if username in active_chats:
        del active_chats[username]
    return redirect('/login')

@app.route('/messages/<username>')
def get_messages(username):
    current_user = session.get('username')
    if not current_user or current_user not in users:
        return jsonify({'error': 'Unauthorized'}), 401
    
    # Mark messages as read when fetched
    if username in users[current_user]['unread']:
        users[current_user]['unread'][username] = 0
        socketio.emit('unread_update', {
            'user': current_user,
            'with_user': username,
            'count': 0
        })
    
    # Get all messages and files
    messages = users[current_user]['messages'].get(username, [])
    files = users[current_user]['files'].get(username, [])
    
    # Combine and sort by timestamp
    all_messages = messages + files
    all_messages.sort(key=lambda x: datetime.strptime(x['time'], '%Y-%m-%d %H:%M:%S') if 'time' in x else datetime.min)
    
    return jsonify({'messages': all_messages})

@app.route('/download/<filename>')
def download_file(filename):
    if 'username' not in session:
        return 'Unauthorized', 401
    
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    
    if not os.path.exists(filepath):
        return 'File not found', 404
    
    # Handle range requests for media files
    range_header = request.headers.get('Range')
    if not range_header:
        try:
            return send_from_directory(
                app.config['UPLOAD_FOLDER'],
                filename,
                as_attachment=False,
                conditional=True
            )
        except ConnectionError:
            return '', 204
    
    # Parse range header
    size = os.path.getsize(filepath)
    byte1, byte2 = 0, None
    
    range_ = range_header.replace('bytes=', '').split('-')
    byte1 = int(range_[0])
    if range_[1]:
        byte2 = int(range_[1])
    
    length = size - byte1
    if byte2 is not None:
        length = byte2 - byte1 + 1
    
    # Read file chunk
    data = None
    with open(filepath, 'rb') as f:
        f.seek(byte1)
        data = f.read(length)
    
    # Prepare response
    rv = Response(
        data,
        206,
        mimetype=mimetypes.guess_type(filename)[0] or 'application/octet-stream',
        direct_passthrough=True
    )
    
    rv.headers.add('Content-Range', f'bytes {byte1}-{byte1 + length - 1}/{size}')
    rv.headers.add('Accept-Ranges', 'bytes')
    rv.headers.add('Content-Length', str(length))
    
    return rv

@app.route('/upload', methods=['POST'])
def upload_file():
    if 'username' not in session:
        return jsonify({'status': 'error', 'message': 'Unauthorized'}), 401
    
    sender = session['username']
    receiver = request.form.get('receiver')
    file = request.files.get('file')
    
    if not receiver or not file or receiver not in users:
        return jsonify({'status': 'error', 'message': 'Invalid request'}), 400
    
    if file and allowed_file(file.filename):
        filename = secure_filename(f"{sender}_{receiver}_{datetime.now().timestamp()}_{file.filename}")
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        file.save(filepath)
        
        # Determine file type
        file_type = mimetypes.guess_type(filename)[0] or 'application/octet-stream'
        if file_type.startswith('image'):
            file_type = 'image'
        elif file_type.startswith('video'):
            file_type = 'video'
        elif file_type.startswith('audio'):
            file_type = 'audio'
        else:
            file_type = 'file'
        
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
        file_data = {
            'from': sender,
            'filename': filename,
            'original_name': file.filename,
            'time': timestamp,
            'type': file_type,
            'id': str(datetime.now().timestamp())
        }
        
        # Store file reference
        users[sender]['files'].setdefault(receiver, []).append(file_data)
        users[receiver]['files'].setdefault(sender, []).append(file_data)
        
        # Send to sender (optimistic update)
        socketio.emit('receive_message', file_data, room=online_users[sender])
        
        # Send to receiver if online
        if receiver in online_users:
            if active_chats.get(receiver) != sender:
                users[receiver]['unread'][sender] = users[receiver]['unread'].get(sender, 0) + 1
                socketio.emit('unread_update', {
                    'user': receiver,
                    'with_user': sender,
                    'count': users[receiver]['unread'][sender]
                }, room=online_users[receiver])
                socketio.emit('play_notification', {}, room=online_users[receiver])
            
            socketio.emit('receive_message', file_data, room=online_users[receiver])
        
        return jsonify({'status': 'success', 'filename': filename, 'file_type': file_type})
    
    return jsonify({'status': 'error', 'message': 'Invalid file type'}), 400

@app.route('/delete_message', methods=['POST'])
def delete_message():
    if 'username' not in session:
        return jsonify({'status': 'error', 'message': 'Unauthorized'}), 401
    
    data = request.get_json()
    sender = session['username']
    receiver = data.get('receiver')
    message_id = data.get('message_id')
    delete_for = data.get('delete_for', 'me')  # 'me' or 'everyone'
    
    if not receiver or not message_id or receiver not in users:
        return jsonify({'status': 'error', 'message': 'Invalid request'}), 400
    
    # Find and delete the message
    deleted = False
    
    # Search in text messages
    if delete_for == 'me':
        # Only delete for the requesting user
        if receiver in users[sender]['messages']:
            users[sender]['messages'][receiver] = [
                msg for msg in users[sender]['messages'][receiver] 
                if not (msg.get('id') == message_id)
            ]
            deleted = True
    else:
        # Delete for everyone
        if receiver in users[sender]['messages']:
            users[sender]['messages'][receiver] = [
                msg for msg in users[sender]['messages'][receiver] 
                if msg.get('id') != message_id
            ]
            deleted = True
        
        if sender in users[receiver]['messages']:
            users[receiver]['messages'][sender] = [
                msg for msg in users[receiver]['messages'][sender] 
                if msg.get('id') != message_id
            ]
            deleted = True
    
    # Search in files (similar logic)
    if delete_for == 'me':
        if receiver in users[sender]['files']:
            users[sender]['files'][receiver] = [
                f for f in users[sender]['files'][receiver] 
                if not (f.get('id') == message_id)
            ]
            deleted = True
    else:
        if receiver in users[sender]['files']:
            users[sender]['files'][receiver] = [
                f for f in users[sender]['files'][receiver] 
                if f.get('id') != message_id
            ]
            deleted = True
        
        if sender in users[receiver]['files']:
            users[receiver]['files'][sender] = [
                f for f in users[receiver]['files'][sender] 
                if f.get('id') != message_id
            ]
            deleted = True
    
    if deleted:
        # Notify clients
        socketio.emit('message_deleted', {
            'message_id': message_id,
            'deleted_for': delete_for,
            'sender': sender,
            'receiver': receiver
        }, room=online_users[sender])
        
        if receiver in online_users:
            socketio.emit('message_deleted', {
                'message_id': message_id,
                'deleted_for': delete_for,
                'sender': sender,
                'receiver': receiver
            }, room=online_users[receiver])
        
        return jsonify({'status': 'success'})
    
    return jsonify({'status': 'error', 'message': 'Message not found'}), 404

@socketio.on('connect')
def handle_connect():
    if 'username' in session:
        username = session['username']
        online_users[username] = request.sid
        emit('user_status', {'username': username, 'status': 'online'}, broadcast=True)

@socketio.on('disconnect')
def handle_disconnect():
    username = session.get('username')
    if username and username in online_users:
        del online_users[username]
        emit('user_status', {'username': username, 'status': 'offline'}, broadcast=True)
    if username in active_chats:
        del active_chats[username]

@socketio.on('set_active_chat')
def handle_set_active_chat(data):
    username = session.get('username')
    if username:
        active_chats[username] = data['with_user']
        if data['with_user'] in users[username]['unread']:
            users[username]['unread'][data['with_user']] = 0
            emit('unread_update', {
                'user': username,
                'with_user': data['with_user'],
                'count': 0
            })

@socketio.on('private_message')
def handle_private_message(data):
    sender = session.get('username')
    receiver = data.get('to')
    message = data.get('message')
    message_id = data.get('message_id', str(datetime.now().timestamp()))
    
    if not sender or not receiver or not message or receiver not in users:
        return
    
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    msg_data = {
        'from': sender, 
        'message': message, 
        'time': timestamp, 
        'type': 'text',
        'id': message_id
    }
    
    # Initialize if not exists
    if receiver not in users[sender]['messages']:
        users[sender]['messages'][receiver] = []
    if sender not in users[receiver]['messages']:
        users[receiver]['messages'][sender] = []
    
    # Add message to both users
    users[sender]['messages'][receiver].append(msg_data)
    users[receiver]['messages'][sender].append(msg_data)
    
    # Send to sender (optimistic update already done)
    emit('receive_message', msg_data, room=online_users[sender])
    
    # Send to receiver if online
    if receiver in online_users:
        # Increment unread if not active chat
        if active_chats.get(receiver) != sender:
            users[receiver]['unread'][sender] = users[receiver]['unread'].get(sender, 0) + 1
            emit('unread_update', {
                'user': receiver,
                'with_user': sender,
                'count': users[receiver]['unread'][sender]
            }, room=online_users[receiver])
            emit('play_notification', {}, room=online_users[receiver])
        
        emit('receive_message', msg_data, room=online_users[receiver])

@socketio.on('message_deleted')
def handle_message_deleted(data):
    # Just pass it through to the clients
    emit('message_deleted', data, room=online_users[data['receiver']])
    if data['sender'] in online_users:
        emit('message_deleted', data, room=online_users[data['sender']])

if __name__ == '__main__':
    socketio.run(app, debug=True)