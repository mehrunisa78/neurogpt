from flask import Flask, render_template, request, jsonify, session, redirect, url_for, Response
import json
import random
import openai
import os
import psycopg2
from dotenv import load_dotenv
from werkzeug.security import generate_password_hash, check_password_hash
from flask_mail import Mail, Message
from itsdangerous import URLSafeTimedSerializer
from psycopg2.errors import UniqueViolation

app = Flask(__name__)
app.secret_key = 'neurogpt_secret_786_random_key'

# Load environment variables
load_dotenv()
openai.api_key = os.getenv("OPENAI_API_KEY")

# Mail config
app.config['MAIL_SERVER'] = 'smtp.gmail.com'
app.config['MAIL_PORT'] = 587
app.config['MAIL_USE_TLS'] = True
app.config['MAIL_USERNAME'] = os.getenv("MAIL_USERNAME")
app.config['MAIL_PASSWORD'] = os.getenv("MAIL_PASSWORD")
app.config['MAIL_DEFAULT_SENDER'] = os.getenv("MAIL_USERNAME")

mail = Mail(app)

# Token serializer
serializer = URLSafeTimedSerializer(app.secret_key)

def get_pg_connection():
    return psycopg2.connect(
        dbname=os.getenv("DB_NAME"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASS"),
        host=os.getenv("DB_HOST"),
        port="5432",
        sslmode="require"
    )

# ------------------ JSON LOADERS ------------------ #
def safe_load_json(filename):
    try:
        with open(filename, encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        print(f"❌ Error loading {filename}: {str(e)}")
        return {"intents": []}

def merge_intents(*json_files):
    all_intents = []
    for file in json_files:
        data = safe_load_json(file)
        all_intents.extend(data.get("intents", []))
    return {"intents": all_intents}

prompt_data = safe_load_json('prompts.json')
quiz_data = safe_load_json('quiz.json')
all_intents_data = merge_intents('intents.json', 'emotion.json', 'emotional.json', 'journaling.json', 'neurogptgrowth.json')

intent_lookup = {}
for intent in all_intents_data["intents"]:
    for pattern in intent.get("patterns", []):
        intent_lookup[pattern.lower()] = intent

@app.route('/')
def home():
    return render_template('index.html')

# ------------------ AUTH ------------------ #
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form['email']
        password = request.form['password']
        conn = get_pg_connection()
        c = conn.cursor()
        c.execute("SELECT * FROM users WHERE email = %s", (email,))
        user = c.fetchone()
        conn.close()

        if user:
            print("Fetched user:", user)  # 👈 Debugging
            print("Stored password:", user[2])
            print("Entered password:", password)
            print("Hash match:", check_password_hash(user[2], password))

        if user and check_password_hash(user[3], password):
            session['user'] = email
            return redirect('/chat')
        else:
            return render_template('login.html', error="Invalid credentials")
    return render_template('login.html')


@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form['username']  # 👈 username field added
        email = request.form['email']
        password = request.form['password']
        hashed_pw = generate_password_hash(password)

        try:
            conn = get_pg_connection()
            c = conn.cursor()
            c.execute(
                "INSERT INTO users (username, email, password) VALUES (%s, %s, %s)",
                (username, email, hashed_pw)
            )
            conn.commit()
            conn.close()
            session['user'] = email
            return redirect('/chat')

        except psycopg2.IntegrityError as e:
            conn.rollback()
            conn.close()
            print("❌ DB Error:", e)
            return render_template('register.html', error="Email or username already exists")

        except Exception as e:
            print("❌ Unexpected Error:", e)
            return render_template('register.html', error="Something went wrong. Please try again.")
    
    return render_template('register.html')


@app.route('/logout')
def logout():
    session.clear()
    return redirect('/')

# ------------------ STRIPE SUBSCRIPTION ------------------ #
import stripe
stripe.api_key = os.getenv("STRIPE_SECRET_KEY")

@app.route('/subscribe')
def subscribe():
    if 'user' not in session:
        return redirect('/login')
    checkout_session = stripe.checkout.Session.create(
        payment_method_types=['card'],
        line_items=[{'price': 'price_1RjajVRhlxl5TYRxaox1Gl3A', 'quantity': 1}],
        mode='payment',
        success_url=url_for('payment_success', _external=True),
        cancel_url=url_for('payment_cancel', _external=True),
    )
    return redirect(checkout_session.url, code=303)

@app.route('/payment_success')
def payment_success():
    if 'user' in session:
        with get_pg_connection() as conn:
            c = conn.cursor()
            c.execute("UPDATE users SET subscribed = TRUE WHERE email = %s", (session['user'],))
            conn.commit()
        msg = Message("✅ NeuroGPT Pro Activated", recipients=[session['user']])
        msg.body = "Thanks for subscribing to NeuroGPT Pro! Enjoy unlimited chat messages 🚀"
        mail.send(msg)
    return render_template('payment_success.html')

@app.route('/payment_cancel')
def payment_cancel():
    return render_template('payment_cancel.html')

@app.route('/chat')
def chat():
    if 'user' not in session:
        return redirect('/login')

    email = session['user']
    conn = get_pg_connection()
    c = conn.cursor()
    c.execute("SELECT subscribed, username FROM users WHERE email = %s", (email,))
    row = c.fetchone()
    conn.close()

    isProUser = row and row[0]
    username = row[1] if row else "there"
    return render_template('chat.html', isProUser=isProUser, username=username)


# ------------------ CHATBOT STREAM ------------------ #
@app.route('/stream-reply', methods=['POST'])
def stream_reply():
    data = request.get_json()
    user_input = data.get("title", "").strip()
    def generate():
        try:
            response = openai.chat.completions.create(
                model="gpt-4",
                messages=[{"role": "system", "content": "You are a friendly growth mindset assistant."},
                          {"role": "user", "content": user_input}],
                max_tokens=200,
                temperature=0.7,
                stream=True)
            for chunk in response:
                if chunk.choices and chunk.choices[0].delta.content:
                    yield f"data: {chunk.choices[0].delta.content}\n\n"
        except Exception as e:
            yield f"data: [ERROR] {str(e)}\n\n"
    return Response(generate(), mimetype='text/event-stream')

# ------------------ MAIN BOT LOGIC ------------------ #
@app.route('/get-reply', methods=['POST'])
def get_reply():
    data = request.get_json()
    user_input = data.get("title", "").lower().strip()
    if 'user' not in session:
        return jsonify({"bot_response": "⚠️ Login required.", "user_message": ""})

    email = session['user']
    conn = get_pg_connection()
    c = conn.cursor()
    c.execute("SELECT message_count, subscribed FROM users WHERE email = %s", (email,))
    row = c.fetchone()

    if not row:
        conn.close()
        return jsonify({"bot_response": "⚠️ User not found.", "user_message": ""})

    message_count, subscribed = row
    if not subscribed:
        if message_count >= 4:
            conn.close()
            return jsonify({
                "limit_reached": True,
                "bot_response": "🚫 You’ve reached your free message limit. Please [subscribe](/subscribe) to continue chatting."
            })
        c.execute("UPDATE users SET message_count = message_count + 1 WHERE email = %s", (email,))
        conn.commit()

    conn.close()

    if "self-assessment" in user_input or "quiz" in user_input:
        return jsonify({"type": "quiz"})

    for prompt in prompt_data.get("prompt_menu", {}).get("prompts", []):
        if prompt["title"].lower() == user_input:
            return jsonify({"user_message": prompt["user_message"], "bot_response": prompt["bot_response"]})

    yes_inputs = ["yes", "yes please", "okay", "sure", "i want", "i want it", "yeah", "yep"]
    last_context = session.get("last_context")

    if user_input in yes_inputs:
        if last_context == "offer_7_day_plan":
            session["last_context"] = None
            return jsonify({
                "user_message": user_input,
                "bot_response": "📅 Here's your 7-day mindset challenge:\n\nDay 1: Reframe 3 negative thoughts\nDay 2: Ask for feedback\nDay 3: Do something scary\nDay 4: Reflect on failure\nDay 5: Celebrate a win\nDay 6: Teach someone\nDay 7: Journal about your growth"
            })
        elif last_context == "offer_starter_plan":
            session["last_context"] = "offer_7_day_plan"
            return jsonify({
                "user_message": user_input,
                "bot_response": "💡 Starter Plan:\n\n1. Replace 'I can't' with 'I can't yet'\n2. Reflect nightly\n3. Set weekly growth goals\n4. Celebrate effort\n\nWant a full 7-day plan too?"
            })
        elif last_context == "offer_tracker":
            session["last_context"] = None
            return jsonify({
                "user_message": user_input,
                "bot_response": "📊 Weekly Tracker:\n- Daily Reflection\n- Feedback Notes\n- Weekly Challenge\n- Confidence Rating (1–5)"
            })

    matched_intent = intent_lookup.get(user_input)
    if matched_intent:
        if matched_intent.get("intent") == "starter_plan":
            session["last_context"] = "offer_7_day_plan"
        elif matched_intent.get("intent") == "mixed_mindset_plan":
            session["last_context"] = "offer_tracker"
        elif matched_intent.get("intent") == "action_plan":
            session["last_context"] = "offer_starter_plan"
        else:
            session["last_context"] = None
        return jsonify({"user_message": user_input,
                        "bot_response": random.choice(matched_intent.get("responses", []))})

    emotion_responses = {
        "anxious": "😟 It’s okay to feel anxious. Try to take a few deep breaths — you’re safe now.",
        "calm": "🧘 I’m glad to hear you’re feeling calm. Hold onto that inner peace.",
        "hopeful": "🌈 Hope is a powerful thing. Keep moving forward — you’re doing great.",
        "angry": "😤 Anger can be a signal. Let’s find a healthy way to release it.",
        "sad": "😢 I'm here for you. It’s okay to feel sad — you’re not alone.",
        "confused": "🤔 It’s okay to not have all the answers. Clarity will come.",
        "tired": "😴 You’ve been doing a lot. Maybe it's time for some rest and self-care.",
        "frustrated": "😣 Frustration means you care. Let's take a step back and breathe.",
        "lonely": "💔 You’re not alone — I’m here with you. Let’s talk.",
        "guilty": "😔 Guilt shows you have a strong conscience. Be kind to yourself too.",
        "relieved": "😌 I'm glad something got easier. You deserve that peace.",
        "excited": "🎉 That’s wonderful! What’s making you feel this way?",
        "overwhelmed": "🌊 It’s okay to pause. Let’s break things down together.",
        "happy": "😊 That’s great! I’m so glad to hear you're feeling happy.",
        "low": "⬇️ Low moments happen. But they don’t define you.",
        "depressed": "💙 You matter. You’re not alone in this. Want to talk about it?",
        "shame": "🫣 Shame isn’t truth. You are worthy just as you are.",
        "jealous": "😒 Jealousy shows us what we value. Let’s explore that with compassion.",
        "stressed": "😬 Stress is heavy. Let’s try a calming strategy together.",
        "insecure": "🫥 Even now, you are enough. You’re growing, even if it’s hard to see.",
        "peaceful": "🌿 That’s beautiful. I hope that peace stays with you.",
        "numb": "🕳️ Numbness is a signal. Let’s reconnect, slowly and gently.",
        "motivated": "🚀 Amazing! Let’s channel that energy toward something you care about."
    }

    for keyword, reply in emotion_responses.items():
        if keyword in user_input:
            return jsonify({"user_message": user_input, "bot_response": reply})

    try:
        response = openai.chat.completions.create(
            model="gpt-4",
            messages=[{"role": "system", "content": "You are a friendly growth mindset assistant."},
                      {"role": "user", "content": user_input}],
            max_tokens=200,
            temperature=0.7)
        gpt_reply = response.choices[0].message.content.strip()
        return jsonify({"user_message": user_input, "bot_response": gpt_reply})
    except:
        return jsonify({"user_message": user_input, "bot_response": "⚠️ Something went wrong."})

@app.route('/get-prompts')
def get_prompts():
    return jsonify(prompt_data.get("prompt_menu", {}).get("prompts", []))

@app.route('/get-quiz')
def get_quiz():
    return jsonify(quiz_data.get("self_assessment_quiz", []))

@app.route('/short-reply', methods=['POST'])
def short_reply():
    data = request.get_json()
    user_input = data.get("title", "").strip()
    try:
        response = openai.chat.completions.create(
            model="gpt-4",
            messages=[{"role": "system", "content": "Reply briefly (1–2 lines) as a motivational mindset assistant."},
                      {"role": "user", "content": user_input}],
            max_tokens=150,
            temperature=0.7)
        return jsonify({"reply": response.choices[0].message.content.strip()})
    except:
        return jsonify({"reply": "⚠️ Error occurred while replying."})

@app.route('/reset-password/<token>', methods=['GET', 'POST'])
def reset_password(token):
    try:
        email = serializer.loads(token, salt='reset-password', max_age=3600)
    except:
        return "❌ Invalid or expired link"
    if request.method == 'POST':
        new_password = request.form['password']
        hashed_pw = generate_password_hash(new_password)
        with get_pg_connection() as conn:
            c = conn.cursor()
            c.execute("UPDATE users SET password = %s WHERE email = %s", (hashed_pw, email))
            conn.commit()
        return redirect('/login')
    return render_template('reset_password.html')

@app.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    if request.method == 'POST':
        email = request.form['email']
        with get_pg_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT * FROM users WHERE email = %s", (email,))
            user = c.fetchone()
            if user:
                token = serializer.dumps(email, salt='reset-password')
                reset_url = url_for('reset_password', token=token, _external=True)
                msg = Message("Reset Your Password", recipients=[email])
                msg.body = f"Click the link to reset your password: {reset_url}"
                mail.send(msg)
                return render_template('forgot_password.html', success="Reset link sent to your email.")
            else:
                return render_template('forgot_password.html', error="Email not found.")
    return render_template('forgot_password.html')
@app.route('/create-tables')
def create_tables():
    conn = get_pg_connection()
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            username VARCHAR(100) UNIQUE NOT NULL,
            email VARCHAR(255) UNIQUE NOT NULL,
            password VARCHAR(255) NOT NULL,
            subscribed BOOLEAN DEFAULT FALSE,
            subscription_start TIMESTAMP,
            message_count INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()
    return "✅ Users table created!"


if __name__ == '__main__':
    app.run(debug=True)
