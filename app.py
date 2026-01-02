import os
import re
from datetime import datetime, timezone
from collections import defaultdict
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request
from flask_sqlalchemy import SQLAlchemy
from flask_socketio import SocketIO, emit, join_room, leave_room

load_dotenv()

db = SQLAlchemy()
socketio = SocketIO(cors_allowed_origins="*")


class Message(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    room = db.Column(db.String(128), index=True, nullable=False)
    sender = db.Column(db.String(64), nullable=False)
    recipient = db.Column(db.String(64))
    content = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(ZoneInfo("Asia/Kolkata")), nullable=False)
    is_read = db.Column(db.Boolean, default=False, nullable=False)


class Mention(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    message_id = db.Column(db.Integer, db.ForeignKey('message.id'), nullable=False)
    mentioned_user = db.Column(db.String(64), nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(ZoneInfo("Asia/Kolkata")), nullable=False)
    
    message = db.relationship('Message', backref=db.backref('mentions', lazy=True))


class Notification(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user = db.Column(db.String(64), nullable=False, index=True)
    type = db.Column(db.String(32), nullable=False)  # 'mention', 'message', etc.
    title = db.Column(db.String(128), nullable=False)
    content = db.Column(db.Text, nullable=False)
    room = db.Column(db.String(128))
    sender = db.Column(db.String(64))
    is_read = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(ZoneInfo("Asia/Kolkata")), nullable=False)


connected_users = {}  # sid -> username
sid_rooms = defaultdict(set)  # sid -> set of room keys


def extract_mentions(content):
    """Extract @mentions from message content"""
    pattern = r'@(\w+)'
    return re.findall(pattern, content)


def create_app():
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "dev-secret")
    app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv("DATABASE_URL", "sqlite:///chat.db")
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    db.init_app(app)
    socketio.init_app(app, async_mode="threading")

    with app.app_context():
        db.create_all()

    register_routes(app)
    register_socket_handlers()
    return app


def register_routes(app: Flask) -> None:
    @app.route("/")
    def index():
        return render_template("index.html")

    @app.get("/api/messages")
    def get_messages():
        room_type = request.args.get("room_type", "group")
        room_name = request.args.get("room_name") or "lobby"
        user = request.args.get("user")
        target = request.args.get("target")

        try:
            key = room_key(room_type, room_name=room_name, user=user, target=target)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

        messages = (
            Message.query.filter_by(room=key)
            .order_by(Message.created_at.asc())
            .limit(200)
            .all()
        )
        return jsonify(
            [
                {
                    "id": m.id,
                    "room": m.room,
                    "sender": m.sender,
                    "recipient": m.recipient,
                    "content": m.content,
                    "created_at": m.created_at.isoformat(),
                    "is_read": m.is_read,
                    "mentions": [mention.mentioned_user for mention in m.mentions]
                }
                for m in messages
            ]
        )

    @app.get("/api/room_users")
    def get_room_users():
        room_type = request.args.get("room_type", "group")
        room_name = request.args.get("room_name") or "lobby"
        user = request.args.get("user")
        target = request.args.get("target")

        try:
            key = room_key(room_type, room_name=room_name, user=user, target=target)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

        # Get users currently in the room
        room_users = set()
        for sid, rooms in sid_rooms.items():
            if key in rooms:
                username = connected_users.get(sid)
                if username:
                    room_users.add(username)
        
        return jsonify({"users": sorted(list(room_users))})

    @app.get("/api/notifications")
    def get_notifications():
        user = request.args.get("user")
        if not user:
            return jsonify({"error": "user parameter required"}), 400
        
        notifications = (
            Notification.query.filter_by(user=user)
            .order_by(Notification.created_at.desc())
            .limit(50)
            .all()
        )
        
        return jsonify([
            {
                "id": n.id,
                "type": n.type,
                "title": n.title,
                "content": n.content,
                "room": n.room,
                "sender": n.sender,
                "is_read": n.is_read,
                "created_at": n.created_at.isoformat()
            }
            for n in notifications
        ])

    @app.post("/api/notifications/read")
    def mark_notifications_read():
        user = request.json.get("user")
        notification_ids = request.json.get("notification_ids", [])
        
        if not user:
            return jsonify({"error": "user parameter required"}), 400
        
        if notification_ids:
            # Mark specific notifications as read
            updated = (
                Notification.query.filter(
                    Notification.id.in_(notification_ids),
                    Notification.user == user,
                    Notification.is_read == False
                )
                .update({"is_read": True})
            )
        else:
            # Mark all notifications as read
            updated = (
                Notification.query.filter_by(user=user, is_read=False)
                .update({"is_read": True})
            )
        
        db.session.commit()
        return jsonify({"updated": updated})


def register_socket_handlers() -> None:
    @socketio.on("connect")
    def on_connect():
        emit("connected", {"message": "connected"})

    @socketio.on("disconnect")
    def on_disconnect():
        username = connected_users.pop(request.sid, None)
        rooms = sid_rooms.pop(request.sid, set())
        
        # Notify all rooms about the updated user lists
        for room_key in rooms:
            room_users = []
            for sid, user_rooms in sid_rooms.items():
                if room_key in user_rooms:
                    room_username = connected_users.get(sid)
                    if room_username:
                        room_users.append(room_username)
            
            emit("room_users_update", {"users": sorted(list(set(room_users)))}, room=room_key)

    @socketio.on("register")
    def on_register(data):
        username = (data or {}).get("username")
        if not username:
            emit("error", {"message": "username is required"})
            return
        connected_users[request.sid] = username
        emit("registered", {"username": username})

    @socketio.on("join_room")
    def on_join(data):
        username = connected_users.get(request.sid)
        if not username:
            emit("error", {"message": "register first"})
            return
        room_type = (data or {}).get("room_type", "group")
        room_name = (data or {}).get("room_name") or "lobby"
        target = (data or {}).get("target")

        try:
            key = room_key(room_type, room_name=room_name, user=username, target=target)
        except ValueError as exc:
            emit("error", {"message": str(exc)})
            return

        join_room(key)
        sid_rooms[request.sid].add(key)
        emit("room_joined", {"room": key})
        
        # Notify all users in the room about the updated user list
        room_users = []
        for sid, rooms in sid_rooms.items():
            if key in rooms:
                room_username = connected_users.get(sid)
                if room_username:
                    room_users.append(room_username)
        
        emit("room_users_update", {"users": sorted(list(set(room_users)))}, room=key)

    @socketio.on("leave_room")
    def on_leave(data):
        username = connected_users.get(request.sid)
        if not username:
            emit("error", {"message": "register first"})
            return
        room_type = (data or {}).get("room_type", "group")
        room_name = (data or {}).get("room_name") or "lobby"
        target = (data or {}).get("target")
        try:
            key = room_key(room_type, room_name=room_name, user=username, target=target)
        except ValueError as exc:
            emit("error", {"message": str(exc)})
            return
        leave_room(key)
        sid_rooms[request.sid].discard(key)
        emit("room_left", {"room": key})
        
        # Notify all users in the room about the updated user list
        room_users = []
        for sid, rooms in sid_rooms.items():
            if key in rooms:
                room_username = connected_users.get(sid)
                if room_username:
                    room_users.append(room_username)
        
        emit("room_users_update", {"users": sorted(list(set(room_users)))}, room=key)

    @socketio.on("send_message")
    def on_send_message(data):
        username = connected_users.get(request.sid)
        if not username:
            emit("error", {"message": "register first"})
            return
        content = (data or {}).get("content")
        if not content:
            emit("error", {"message": "content required"})
            return
        room_type = (data or {}).get("room_type", "group")
        room_name = (data or {}).get("room_name") or "lobby"
        target = (data or {}).get("target")

        try:
            key = room_key(room_type, room_name=room_name, user=username, target=target)
        except ValueError as exc:
            emit("error", {"message": str(exc)})
            return
        if key not in sid_rooms.get(request.sid, set()):
            emit("error", {"message": "join the room before sending"})
            return

        recipient = target if room_type == "dm" else None
        message = Message(room=key, sender=username, recipient=recipient, content=content)
        db.session.add(message)
        db.session.commit()

        # Extract and create mentions
        mentioned_users = extract_mentions(content)
        for mentioned_user in mentioned_users:
            mention = Mention(message_id=message.id, mentioned_user=mentioned_user)
            db.session.add(mention)
        db.session.commit()

        payload = {
            "id": message.id,
            "room": key,
            "sender": username,
            "recipient": recipient,
            "content": content,
            "created_at": message.created_at.isoformat(),
            "is_read": message.is_read,
            "mentions": mentioned_users,
        }
        emit("message", payload, room=key)
        
        # Send mention notifications to mentioned users
        for mentioned_user in mentioned_users:
            # Create notification in database
            if room_type == "group":
                title = f"You were mentioned by {username} in group: {room_name}"
            else:
                title = f"You were mentioned by {username} in direct message"
            
            notification = Notification(
                user=mentioned_user,
                type="mention",
                title=title,
                content=content,
                room=key,
                sender=username
            )
            db.session.add(notification)
            
            # Send real-time notification
            for sid, connected_username in connected_users.items():
                if connected_username == mentioned_user:
                    emit("mention_notification", {
                        "message_id": message.id,
                        "sender": username,
                        "content": content,
                        "room": key,
                        "created_at": message.created_at.isoformat()
                    }, room=sid)
                    
                    # Send notification count update
                    emit("notification_update", {
                        "type": "new_notification",
                        "count": Notification.query.filter_by(user=mentioned_user, is_read=False).count()
                    }, room=sid)
        
        db.session.commit()

    @socketio.on("mark_read")
    def on_mark_read(data):
        username = connected_users.get(request.sid)
        if not username:
            emit("error", {"message": "register first"})
            return
        room_type = (data or {}).get("room_type", "group")
        room_name = (data or {}).get("room_name") or "lobby"
        target = (data or {}).get("target")
        try:
            key = room_key(room_type, room_name=room_name, user=username, target=target)
        except ValueError as exc:
            emit("error", {"message": str(exc)})
            return

        if room_type == "dm":
            updated = (
                Message.query.filter_by(room=key, recipient=username, is_read=False)
                .update({"is_read": True})
            )
            db.session.commit()
            emit("read_receipt", {"room": key, "count": updated}, room=key)
        else:
            emit("read_receipt", {"room": key, "count": 0})


def room_key(room_type: str, room_name: str | None, user: str | None, target: str | None) -> str:
    if room_type == "group":
        return f"group:{room_name or 'lobby'}"
    if room_type == "dm":
        if not user or not target:
            raise ValueError("user and target required for direct messages")
        first, second = sorted([user, target])
        return f"dm:{first}:{second}"
    raise ValueError("unknown room type")


app = create_app()


if __name__ == "__main__":
    port = int(os.getenv("PORT", "7812"))
    
    # For HTTPS support, uncomment these lines and provide your certificate files
    ssl_context = ('cert.pem', 'key.pem')
    socketio.run(app, host="0.0.0.0", port=port, debug=True, ssl_context=ssl_context)
