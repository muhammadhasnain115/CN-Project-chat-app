from flask import Flask, render_template, request, redirect, session, url_for
from flask_socketio import SocketIO, emit
import os
from ftplib import FTP

app = Flask(__name__)
app.secret_key = 'supersecretkey'
socketio = SocketIO(app)

# FTP Config
FTP_HOST = 'localhost'
FTP_USER = 'ftpuser'
FTP_PASS = 'ftppass'
FTP_UPLOAD_DIR = '/uploads'
UPLOAD_FOLDER = 'uploads'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# In-memory user storage and messages
users = {}  # username: {password, messages: {receiver: [messages]}, files: {receiver: [files]}}
online_users = {}  # {username: sid}

@app.route('/')
def index():
    username = session.get('username')
    if not username or username not in users:
        return redirect('/login')  # Redirect to login if user is not logged in or not found in users

    user_messages = users[username]['messages']
    user_files = users[username]['files']
    return render_template('chat.html', username=username, users=[u for u in users if u != session['username']], 
                           messages=user_messages, files=user_files)



@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        if username in users:
            return 'Username already exists.', 400
        # Initialize user entry
        users[username] = {'password': password, 'messages': {}, 'files': {}}
        session['username'] = username
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
        return 'Invalid credentials.', 401
    return render_template('login.html')


@app.route('/logout')
def logout():
    username = session.pop('username', None)
    if username in online_users:
        del online_users[username]
    return redirect('/login')

@app.route('/messages/<username>')
def get_messages(username):
    current_user = session.get('username')
    if current_user and current_user in users:
        user_messages = users[current_user]['messages']
        if username in user_messages:
            return {'messages': user_messages[username]}, 200
    return {'messages': []}, 404


@app.route('/download/<filename>')
def download_file(filename):
    filepath = os.path.join(UPLOAD_FOLDER, filename)
    if os.path.exists(filepath):
        return send_from_directory(UPLOAD_FOLDER, filename)
    return 'File not found', 404


@app.route('/upload', methods=['POST'])
def upload():
    sender = session.get('username')
    receiver = request.form.get('receiver')
    file = request.files['file']
    if file:
        filepath = os.path.join(UPLOAD_FOLDER, file.filename)
        file.save(filepath)

        ftp = FTP(FTP_HOST)
        ftp.login(FTP_USER, FTP_PASS)
        ftp.cwd(FTP_UPLOAD_DIR)
        with open(filepath, 'rb') as f:
            ftp.storbinary(f'STOR {file.filename}', f)
        ftp.quit()

        # Store the file for offline users
        if receiver in users:
            users[receiver]['files'].setdefault(sender, []).append(file.filename)
        
        # Emit to the receiver if online
        if receiver in online_users:
            socketio.emit('receive_file', {'sender': sender, 'filename': file.filename}, room=online_users[receiver])
        
        os.remove(filepath)
        return 'File sent via FTP.'
    return 'Receiver or file missing.', 400

@socketio.on('connect')
def handle_connect():
    username = session.get('username')
    if username:
        online_users[username] = request.sid
        # Send all offline messages to the user
        if username in users:
            user_messages = users[username]['messages']
            for sender, messages in user_messages.items():
                for message in messages:
                    emit('receive_private_message', {'from': sender, 'message': message['message']})
            
            # Send all offline files to the user
            user_files = users[username]['files']
            for sender, files in user_files.items():
                for file in files:
                    emit('receive_file', {'sender': sender, 'filename': file})

@socketio.on('disconnect')
def handle_disconnect():
    username = session.get('username')
    if username in online_users:
        del online_users[username]

@socketio.on('private_message')
def handle_private_message(data):
    sender = session.get('username')
    recipient = data['to']
    message = data['message']
    if recipient in users:
        # Store the message for the recipient (whether online or offline)
        users[sender]['messages'].setdefault(recipient, []).append({'from': sender, 'message': message})
        users[recipient]['messages'].setdefault(sender, []).append({'from': sender, 'message': message})

        # Emit to the recipient if online
        if recipient in online_users:
            emit('receive_private_message', {'from': sender, 'message': message}, room=online_users[recipient])

if __name__ == '__main__':
    socketio.run(app, debug=True)
