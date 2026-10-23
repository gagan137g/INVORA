from flask import Flask, render_template, request, redirect, session, jsonify, flash
import mysql.connector
from werkzeug.security import generate_password_hash, check_password_hash
import os
import urllib.parse
import re
import random
import sqlite3
import time
import datetime
from datetime import date, datetime as dt
from werkzeug.utils import secure_filename
from PyPDF2 import PdfReader
from google import genai
import requests

app = Flask(__name__)
app.secret_key = "invora_secret_key"
UPLOAD_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploads")
ALLOWED_EXTENSIONS = {"pdf"}

app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# Google GenAI Client
client = genai.Client(api_key="AQ.Ab8RN6LaZhaPjGnHsw9-cC6smM5NGCrt9G0Q2MlVrdmOPDNHMA")
# =============================================================
# DATABASE CONNECTION & UNIFIED HELPER
# =============================================================
def get_db_connection():
    try:
        return mysql.connector.connect(
            host=os.environ.get("DB_HOST", "localhost"),
            user=os.environ.get("DB_USER", "root"),
            password=os.environ.get("DB_PASSWORD", "gagan@2006"),
            database=os.environ.get("DB_NAME", "invora_db")
        )
    except Exception:
        # Cloud / Local Fallback: SQLite
        db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "invora.db")
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn

def init_db():
    conn = get_db_connection()
    if isinstance(conn, sqlite3.Connection):
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                email TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                file_name TEXT NOT NULL,
                page_count INTEGER DEFAULT 0,
                summary_text TEXT,
                notes_text TEXT,
                uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS quiz_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                document_id INTEGER,
                score INTEGER NOT NULL,
                total_questions INTEGER NOT NULL,
                score_percentage INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS study_plans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                plan_title TEXT NOT NULL,
                scheduled_date TEXT,
                start_time TEXT,
                end_time TEXT
            )
        """)
        conn.commit()
    conn.close()

init_db()

def query_db(query, args=(), one=False, commit=False):
    """
    Executes queries seamlessly across MySQL (%s) and SQLite (?).
    Returns dictionary items so row['name'] always works.
    """
    conn = get_db_connection()
    is_sqlite = isinstance(conn, sqlite3.Connection)

    if is_sqlite:
        query = query.replace("%s", "?")
        # Replace MySQL specific date/time functions with SQLite equivalents
        query = re.sub(r"DATE_FORMAT\(([^,]+),\s*['\"][^'\"]+['\"]\)", r"strftime('%d %b %Y', \1)", query, flags=re.IGNORECASE)
        query = re.sub(r"TIME_FORMAT\(([^,]+),\s*['\"][^'\"]+['\"]\)", r"strftime('%H:%M', \1)", query, flags=re.IGNORECASE)
        query = query.replace("CURDATE()", "date('now')")
        cursor = conn.cursor()
    else:
        cursor = conn.cursor(dictionary=True)

    cursor.execute(query, args)

    if commit:
        conn.commit()
        last_id = cursor.lastrowid
        cursor.close()
        conn.close()
        return last_id

    rv = cursor.fetchall()
    cursor.close()
    conn.close()

    if is_sqlite and rv:
        rv = [dict(row) for row in rv]

    return (rv[0] if rv else None) if one else rv

# =============================================================
# FILE & NLP HELPERS
# =============================================================
def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

def extract_text_and_pages(pdf_path):
    text = ""
    try:
        reader = PdfReader(pdf_path)
        total_pages = len(reader.pages)
        for page in reader.pages:
            page_text = page.extract_text()
            if page_text:
                text += page_text + "\n"
        return text, total_pages
    except Exception as e:
        print(f"Error extracting PDF: {e}")
        return "", 1

STOP_WORDS = {
    "the", "and", "is", "in", "it", "of", "to", "a", "an", "this", "that",
    "these", "those", "with", "as", "for", "on", "by", "at", "from", "be",
    "are", "was", "were", "or", "which", "into", "has", "have", "had", "not",
    "such", "also", "each", "other", "than", "then", "more", "most", "about"
}

def ask_local_backup(prompt):
    try:
        res = requests.post(
            "http://localhost:11434/api/generate",
            json={
                "model": "qwen2.5:0.5b",
                "prompt": prompt,
                "stream": False
            },
            timeout=15
        )
        if res.status_code == 200:
            return res.json().get("response", "").strip()
    except Exception:
        pass
    return None

def get_ai_response(prompt):
    candidate_models = [
        "gemini-2.5-flash",
        "gemini-1.5-flash",
        "gemini-2.0-flash"
    ]

    for model_name in candidate_models:
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=prompt
            )
            if response and response.text:
                return response.text.strip()
        except Exception as e:
            print(f"[Gemini {model_name} Error]: {e}")
            continue

    local_output = ask_local_backup(prompt)
    if local_output:
        return local_output

    return None

# =============================================================
# DETERMINISTIC SUMMARIZERS
# =============================================================
def generate_offline_summary(text):
    clean_text = re.sub(r'\s+', ' ', text).strip()
    sentences = [s.strip() for s in re.split(r'(?<=[.!?])\s+', clean_text) if len(s.split()) >= 6]
    words = re.findall(r'\b[A-Za-z]{3,}\b', clean_text.lower())
    freq = {w: words.count(w) for w in set(words) if w not in STOP_WORDS}
    scored = sorted(sentences, key=lambda s: sum(freq.get(w.lower(), 0) for w in re.findall(r'\b[A-Za-z]{3,}\b', s)), reverse=True)

    overview = " ".join([s for s in scored[:2]]) if len(scored) >= 2 else "Document indexed and structured for academic review."
    concepts = [f"• {s}" for s in scored[2:6]] if len(scored) >= 6 else [f"• Core Theory: {overview}"]
    mechanisms = [f"• {s}" for s in scored[6:9]] if len(scored) >= 9 else ["• Key Workflow: Review core chapter definitions and module procedures."]
    takeaways = [f"• {s}" for s in scored[9:12]] if len(scored) >= 12 else ["• High-Yield Focus: Review definitions and primary test cases."]

    output = f"OVERVIEW:\n{overview}\n\n"
    output += "KEY CONCEPTS:\n" + "\n".join(concepts) + "\n\n"
    output += "IMPORTANT MECHANISMS & WORKFLOWS:\n" + "\n".join(mechanisms) + "\n\n"
    output += "KEY TAKEAWAYS & EXAM FOCUS:\n" + "\n".join(takeaways)
    return output

def generate_offline_notes(text):
    clean_text = re.sub(r'\s+', ' ', text).strip()
    sentences = [s.strip() for s in re.split(r'(?<=[.!?])\s+', clean_text) if len(s.split()) >= 6]
    words = re.findall(r'\b[A-Za-z]{3,}\b', clean_text.lower())
    freq = {w: words.count(w) for w in set(words) if w not in STOP_WORDS}
    scored = sorted(sentences, key=lambda s: sum(freq.get(w.lower(), 0) for w in re.findall(r'\b[A-Za-z]{3,}\b', s)), reverse=True)

    notes = "1. THEORETICAL ARCHITECTURE & FOUNDATIONS\n"
    for s in scored[:3]:
        notes += f"• {s}\n"
    notes += "\n2. STEP-BY-STEP TECHNICAL WORKFLOWS\n"
    for s in scored[3:6]:
        notes += f"• {s}\n"
    notes += "\n3. EXHAUSTIVE TERMINOLOGY GLOSSARY\n"
    for s in scored[6:9]:
        notes += f"• {s}\n"
    return notes

def generate_study_summary(text):
    if session.get("engine_mode") == "offline":
        return generate_offline_summary(text)

    summary_prompt = f"""You are INVORA, an expert academic study assistant.
Generate a structured study summary from this text. Plain text and bullet points (•) only.

OVERVIEW:
A 4 to 5 sentence overview of the topic.

KEY CONCEPTS:
- 4 to 5 fundamental terms with clear definitions.

IMPORTANT MECHANISMS & WORKFLOWS:
- Technical workflows and step-by-step logic.

KEY TAKEAWAYS & EXAM FOCUS:
- Critical high-probability exam points.

Source:
{text[:7000]}"""

    res = get_ai_response(summary_prompt)
    return res if res else generate_offline_summary(text)

def generate_study_notes(text):
    if session.get("engine_mode") == "offline":
        return generate_offline_notes(text)

    notes_prompt = f"""You are INVORA, an expert study assistant.
Generate exhaustive lecture notes from this material without Markdown asterisks (** or *).

1. THEORETICAL ARCHITECTURE & FOUNDATIONS
2. STEP-BY-STEP TECHNICAL WORKFLOWS
3. EXHAUSTIVE TERMINOLOGY GLOSSARY

Source:
{text[:7000]}"""

    res = get_ai_response(notes_prompt)
    return res if res else generate_offline_notes(text)

# =============================================================
# CHAPTER & QUIZ GENERATORS
# =============================================================
def get_pdf_chapters_and_text(pdf_path):
    full_text = ""
    extracted_pages = []
    try:
        reader = PdfReader(pdf_path)
        total_pages = len(reader.pages)

        for p_idx in range(total_pages):
            p_text = reader.pages[p_idx].extract_text() or ""
            extracted_pages.append(p_text)
            full_text += p_text + "\n"

        num_chapters = min(max(3, total_pages // 5), 18)
        pages_per_ch = max(1, len(extracted_pages) // num_chapters)
        formatted_chapters = []

        for idx in range(num_chapters):
            start_p = idx * pages_per_ch
            end_p = len(extracted_pages) if idx == num_chapters - 1 else (idx + 1) * pages_per_ch
            chapter_pages = extracted_pages[start_p:end_p]
            slice_text = "\n".join([p for p in chapter_pages if p.strip()])

            if len(slice_text.split()) < 30:
                slice_text = f"Chapter {idx + 1} content covering core syllabus pages {start_p + 1} to {end_p}. " + slice_text

            formatted_chapters.append({
                "title": f"Chapter {idx + 1}",
                "text": slice_text,
                "chapter_num": idx + 1
            })

        return formatted_chapters, full_text
    except Exception as e:
        print(f"[PDF Extraction Error]: {e}")
        return [{"title": "Chapter 1", "text": "Curriculum overview.", "chapter_num": 1}], full_text

def generate_academic_mcqs(text, chapter_title="Chapter 1"):
    rng = random.Random(hash(chapter_title))
    clean = re.sub(r'\s+', ' ', text)
    sentences = [s.strip() for s in re.split(r'(?<=[.!?])\s+', clean) if 20 < len(s.strip()) < 220]

    potential_terms = re.findall(r'\b[A-Z][a-zA-Z0-9_\-]{2,}(?:\s+[A-Z][a-zA-Z0-9_\-]+){0,2}\b', clean)
    filtered_terms = list(dict.fromkeys([
        t.strip() for t in potential_terms 
        if t.lower() not in STOP_WORDS and len(t.strip()) > 3 and not t.isdigit()
    ]))

    distractor_pool = [
        "Symmetric Encryption", "Public Key Infrastructure", "Hash Digest", "Diffie-Hellman Key Exchange",
        "RSA Cryptosystem", "Elliptic Curve Cryptography", "Digital Signature Standard",
        "Message Authentication Code", "Advanced Encryption Standard (AES)", "Data Encryption Standard (DES)",
        "Block Cipher Mode", "Initialization Vector", "Non-Repudiation Service", "Virtual Memory Page Table"
    ]
    term_universe = list(set(filtered_terms + distractor_pool))
    questions_pool = []
    seen = set()

    for s in sentences:
        m = re.search(r'\b([A-Z][a-zA-Z0-9_\-\s]{2,30})\b\s+(is defined as|refers to|means)\s+([^,.;]{15,120})', s, re.I)
        if m:
            term, desc = m.group(1).strip(), m.group(3).strip()
            wrong = [t for t in term_universe if t.lower() != term.lower()]
            if len(wrong) >= 3:
                q = f"In {chapter_title}, which concept is defined as: \"{desc}\"?"
                if q not in seen:
                    seen.add(q)
                    opts = rng.sample(wrong, 3) + [term]
                    rng.shuffle(opts)
                    questions_pool.append({"question": q, "options": opts, "answer": term})

    if len(questions_pool) < 4:
        for term in filtered_terms[:5]:
            wrong = [t for t in term_universe if t.lower() != term.lower()]
            if len(wrong) >= 3:
                q = f"Which term is highlighted in the study text of {chapter_title}?"
                if q not in seen:
                    seen.add(q)
                    opts = rng.sample(wrong, 3) + [term]
                    rng.shuffle(opts)
                    questions_pool.append({"question": q, "options": opts, "answer": term})

    return questions_pool

# =============================================================
# AUTH ROUTES
# =============================================================
@app.route("/", methods=["GET", "POST"])
def login():
    if "user_id" in session:
        return redirect("/dashboard")

    error = None
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        if not email or not password:
            error = "Please enter both email and password."
            return render_template("login.html", error=error)

        user = query_db("SELECT * FROM users WHERE email = %s", (email,), one=True)

        if user and check_password_hash(user["password"], password):
            session["user_id"] = user["id"]
            session["user_name"] = user["name"]
            return redirect("/dashboard")
        else:
            error = "Invalid email or password. Please try again."

    return render_template("login.html", error=error)

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        if not name or not email or not password:
            flash("All fields are required.")
            return render_template("register.html")

        existing_user = query_db("SELECT id FROM users WHERE email = %s", (email,), one=True)
        if existing_user:
            flash("An account with this email already exists. Please log in.")
            return render_template("register.html")

        hashed_password = generate_password_hash(password)
        try:
            query_db(
                "INSERT INTO users (name, email, password) VALUES (%s, %s, %s)",
                (name, email, hashed_password),
                commit=True
            )
            flash("Registration successful! Please log in.")
            return redirect("/")
        except Exception as e:
            flash(f"Registration error: {e}")
            return render_template("register.html")

    return render_template("register.html")

@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if "user_id" in session:
        return redirect("/dashboard")

    error = None
    success = None

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        new_password = request.form.get("new_password", "")
        confirm_password = request.form.get("confirm_password", "")

        if not email or not new_password or not confirm_password:
            error = "Please fill in all fields."
        elif new_password != confirm_password:
            error = "Passwords do not match."
        elif len(new_password) < 6:
            error = "New password must be at least 6 characters."
        else:
            user = query_db("SELECT id FROM users WHERE email = %s", (email,), one=True)
            if user:
                hashed_pw = generate_password_hash(new_password)
                query_db("UPDATE users SET password = %s WHERE id = %s", (hashed_pw, user["id"]), commit=True)
                success = "Password reset successfully! You can now log in."
            else:
                error = "No account found with this email address."

    return render_template("forgot_password.html", error=error, success=success)

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")

# =============================================================
# DASHBOARD ROUTE
# =============================================================
@app.route("/dashboard")
def dashboard():
    if "user_id" not in session:
        return redirect("/")

    user_id = session["user_id"]

    total_pdf_res = query_db("SELECT COUNT(*) AS total FROM documents WHERE user_id = %s", (user_id,), one=True)
    total_pdfs = total_pdf_res["total"] if total_pdf_res else 0

    quiz_stats = query_db("""
        SELECT COUNT(*) AS total_quizzes, 
               COALESCE(ROUND(AVG(score_percentage)), 0) AS avg_score 
        FROM quiz_results 
        WHERE user_id = %s
    """, (user_id,), one=True)
    total_quizzes = quiz_stats["total_quizzes"] if quiz_stats else 0
    avg_score = quiz_stats["avg_score"] if quiz_stats else 0

    pdf_progress = min(100, int((total_pdfs / 10) * 100)) if total_pdfs > 0 else 0
    quiz_progress = min(100, int((total_quizzes / 10) * 100)) if total_quizzes > 0 else 0
    overall_progress = int((pdf_progress * 0.5) + (quiz_progress * 0.5))

    recent_pdfs = query_db("""
        SELECT id, file_name, 
               DATE_FORMAT(uploaded_at, '%d %b %Y') AS upload_date, 
               page_count 
        FROM documents 
        WHERE user_id = %s 
        ORDER BY uploaded_at DESC 
        LIMIT 4
    """, (user_id,))

    upcoming_plans = query_db("""
        SELECT plan_title, 
               DATE_FORMAT(scheduled_date, '%d') AS day_num,
               TIME_FORMAT(start_time, '%h:%i %p') AS start_t,
               TIME_FORMAT(end_time, '%h:%i %p') AS end_t
        FROM study_plans 
        WHERE user_id = %s AND scheduled_date >= CURDATE()
        ORDER BY scheduled_date ASC, start_time ASC 
        LIMIT 3
    """, (user_id,))

    return render_template(
        "dashboard.html",
        username=session.get("user_name", "Student"),
        total_pdfs=total_pdfs,
        total_quizzes=total_quizzes,
        avg_score=avg_score,
        study_hours=round((total_pdfs * 1.5) + (total_quizzes * 0.4), 1),
        pdf_progress=pdf_progress,
        quiz_progress=quiz_progress,
        overall_progress=overall_progress,
        recent_pdfs=recent_pdfs or [],
        upcoming_plans=upcoming_plans or []
    )

# =============================================================
# UPLOAD & DOCUMENT ROUTES
# =============================================================
@app.route("/upload", methods=["GET", "POST"])
def upload():
    if "user_id" not in session:
        return redirect("/")

    if request.method == "POST":
        file = request.files.get("pdf_file") or request.files.get("pdf")
        if not file or file.filename == "":
            return redirect("/dashboard")

        if not allowed_file(file.filename):
            return "Only PDF files are allowed"

        filename = secure_filename(file.filename)
        filepath = os.path.join(app.config["UPLOAD_FOLDER"], filename)
        file.save(filepath)

        reader = PdfReader(filepath)
        total_pages = len(reader.pages)

        pages_to_read = list(range(total_pages)) if total_pages <= 8 else [0, 1, 2, total_pages // 2, total_pages - 1]
        extracted_chunks = []
        for idx in pages_to_read:
            if idx < total_pages:
                txt = reader.pages[idx].extract_text()
                if txt:
                    clean_p = " ".join(txt.split())
                    if len(clean_p) > 40:
                        extracted_chunks.append(clean_p)

        combined_text = "\n\n".join(extracted_chunks)
        summary_result = generate_study_summary(combined_text)
        notes_result = generate_study_notes(combined_text)

        query_db("""
            INSERT INTO documents (user_id, file_name, page_count, summary_text, notes_text)
            VALUES (%s, %s, %s, %s, %s)
        """, (session["user_id"], filename, total_pages, summary_result, notes_result), commit=True)

        return render_template("upload.html", summary=summary_result, notes=notes_result)

    return render_template("upload.html")

@app.route("/notes")
def notes_hub():
    if "user_id" not in session:
        return redirect("/")

    user_id = session["user_id"]
    docs = query_db("""
        SELECT id, file_name, 
               DATE_FORMAT(uploaded_at, '%d %b %Y') AS upload_date, 
               page_count 
        FROM documents 
        WHERE user_id = %s 
        ORDER BY uploaded_at DESC
    """, (user_id,))

    return render_template("notes_hub.html", docs=docs or [])

@app.route("/document/<int:doc_id>")
def view_document_notes(doc_id):
    if "user_id" not in session:
        return redirect("/")

    user_id = session["user_id"]
    doc = query_db("""
        SELECT id, file_name, summary_text, notes_text, page_count,
               DATE_FORMAT(uploaded_at, '%d %b %Y') AS upload_date
        FROM documents 
        WHERE id = %s AND user_id = %s
    """, (doc_id, user_id), one=True)

    if not doc:
        return redirect("/dashboard")

    return render_template("document_view.html", doc=doc)

# =============================================================
# CHAT, QUIZ, & PLAN ROUTES
# =============================================================
@app.route("/chat", methods=["GET", "POST"])
def chat():
    if "user_id" not in session:
        return redirect("/")

    user_id = session["user_id"]

    if request.method == "POST":
        data = request.get_json() if request.is_json else request.form
        question = data.get("question", "").strip()
        doc_id = data.get("document_id")

        if not question:
            return jsonify({"status": "error", "message": "Question cannot be empty."}), 400

        doc_context = ""
        doc_name = "General Knowledge"

        if doc_id and str(doc_id).isdigit() and int(doc_id) > 0:
            doc_record = query_db("SELECT file_name FROM documents WHERE id = %s AND user_id = %s", (doc_id, user_id), one=True)
            if doc_record:
                doc_name = doc_record["file_name"]
                filepath = os.path.join(app.config["UPLOAD_FOLDER"], doc_name)
                if os.path.exists(filepath):
                    raw_text, _ = extract_text_and_pages(filepath)
                    doc_context = raw_text[:6000]

        prompt = f"""You are INVORA, an expert academic tutor.
Context: {doc_name}
Excerpt: {doc_context if doc_context else 'None'}
Question: {question}
Answer clearly without markdown asterisks (no bolding)."""

        answer = get_ai_response(prompt)
        if not answer:
            answer = "I am currently running in backup mode. Feel free to review your lecture notes and quizzes!"

        return jsonify({
            "status": "success",
            "question": question,
            "answer": answer,
            "doc_name": doc_name
        })

    user_documents = query_db("SELECT id, file_name FROM documents WHERE user_id = %s ORDER BY uploaded_at DESC", (user_id,))
    return render_template("chat.html", documents=user_documents or [])

@app.route("/quiz", methods=["GET", "POST"])
def quiz():
    if "user_id" not in session:
        return redirect("/")

    user_id = session["user_id"]
    all_docs = query_db("SELECT id, file_name FROM documents WHERE user_id = %s ORDER BY uploaded_at DESC", (user_id,))

    if not all_docs:
        return redirect("/dashboard")

    doc_id = request.args.get("doc_id", type=int) or request.form.get("doc_id", type=int)
    selected_doc = next((d for d in all_docs if d["id"] == doc_id), all_docs[0])

    if request.method == "POST" and "action_submit" in request.form:
        total_q = int(request.form.get("total_questions", 0))
        score = 0
        review_data = []

        for i in range(total_q):
            user_pick = request.form.get(f"q_{i}", "Unanswered")
            correct_ans = request.form.get(f"ans_{i}", "")
            q_text = request.form.get(f"q_text_{i}", "")
            is_correct = (user_pick.strip().lower() == correct_ans.strip().lower())
            if is_correct:
                score += 1

            review_data.append({
                "question": q_text,
                "user_answer": user_pick,
                "correct_answer": correct_ans,
                "is_correct": is_correct
            })

        percentage = round((score / total_q * 100), 1) if total_q > 0 else 0

        query_db("""
            INSERT INTO quiz_results (user_id, document_id, score, total_questions, score_percentage)
            VALUES (%s, %s, %s, %s, %s)
        """, (user_id, selected_doc["id"], score, total_q, int(percentage)), commit=True)

        return render_template(
            "quiz_results.html",
            score=score,
            total=total_q,
            percentage=percentage,
            review=review_data,
            doc_name=selected_doc["file_name"]
        )

    filepath = os.path.join(app.config["UPLOAD_FOLDER"], selected_doc["file_name"])
    chapters, raw_text = get_pdf_chapters_and_text(filepath) if os.path.exists(filepath) else ([{"title": "Chapter 1", "text": ""}], "")

    chapter_choice = request.form.get("chapter_index") or request.args.get("chapter_index", "0")
    selected_idx = int(chapter_choice) if str(chapter_choice).isdigit() and 0 <= int(chapter_choice) < len(chapters) else 0

    target_slice = chapters[selected_idx]
    scope_title = target_slice.get("title", f"Chapter {selected_idx + 1}")
    target_text = target_slice.get("text", raw_text)

    questions = generate_academic_mcqs(target_text, chapter_title=scope_title)

    return render_template(
        "quiz.html",
        all_docs=all_docs,
        current_doc=selected_doc,
        doc_name=selected_doc["file_name"],
        chapters=chapters,
        selected_chapter_idx=selected_idx,
        questions=questions,
        scope_label=scope_title
    )

@app.route("/chapter-plan")
@app.route("/chapter-plan/<int:doc_id>")
def chapter_plan(doc_id=None):
    if "user_id" not in session:
        return redirect("/")

    user_id = session["user_id"]
    all_docs = query_db("SELECT id, file_name FROM documents WHERE user_id = %s ORDER BY uploaded_at DESC", (user_id,))
    if not all_docs:
        return redirect("/dashboard")

    selected_doc = next((d for d in all_docs if d["id"] == doc_id), all_docs[0])
    filepath = os.path.join(app.config["UPLOAD_FOLDER"], selected_doc["file_name"])
    chapters, _ = get_pdf_chapters_and_text(filepath) if os.path.exists(filepath) else ([{"title": "Chapter 1", "text": ""}], "")

    schedule = []
    for i, ch in enumerate(chapters, start=1):
        word_count = len(ch.get("text", "").split())
        est_hours = max(1.0, round(word_count / 500, 1))
        schedule.append({
            "day": f"Day {i}",
            "chapter": ch.get("title", f"Chapter {i}"),
            "duration": f"{est_hours} hrs",
            "focus": f"Read theory, structural mechanisms, and take the {ch.get('title', f'Chapter {i}')} assessment."
        })

    return render_template(
        "chapter_plan.html",
        all_docs=all_docs,
        current_doc=selected_doc,
        schedule=schedule,
        total_chapters=len(chapters)
    )

def get_video_recommendations(doc_title):
    clean_title = doc_title.lower().replace(".pdf", "").replace("_", " ").replace("-", " ")
    return [
        {
            "topic": f"Complete Guide to {clean_title.title()}",
            "channel": "Top Academic Sources",
            "search_url": f"https://www.youtube.com/results?search_query={urllib.parse.quote(clean_title + ' lecture')}",
            "tag": "Video Lecture"
        }
    ]

@app.route("/videos/<int:doc_id>")
def recommended_videos(doc_id):
    if "user_id" not in session:
        return redirect("/")

    user_id = session["user_id"]
    doc = query_db("SELECT id, file_name FROM documents WHERE id = %s AND user_id = %s", (doc_id, user_id), one=True)
    all_docs = query_db("SELECT id, file_name FROM documents WHERE user_id = %s ORDER BY uploaded_at DESC", (user_id,))

    if not doc:
        return redirect("/dashboard")

    recommendations = get_video_recommendations(doc["file_name"])
    return render_template(
        "videos.html",
        doc_name=doc["file_name"],
        current_doc_id=doc["id"],
        videos=recommendations,
        all_docs=all_docs or [],
        username=session.get("user_name", "Student")
    )

@app.route("/generate-plan", methods=["GET", "POST"])
def generate_plan():
    if "user_id" not in session:
        return redirect("/")

    user_id = session["user_id"]

    if request.method == "POST":
        doc_id = request.form.get("document_id")
        exam_date_str = request.form.get("exam_date")
        daily_hours = int(request.form.get("daily_hours", 2))

        doc = query_db("SELECT file_name, page_count FROM documents WHERE id = %s AND user_id = %s", (doc_id, user_id), one=True)

        if doc and exam_date_str:
            exam_date = dt.strptime(exam_date_str, "%Y-%m-%d").date()
            days_left = max((exam_date - date.today()).days, 1)
            total_pages = doc["page_count"] or 10
            plan_days = min(days_left, 7)
            pages_per_day = max(1, total_pages // plan_days)

            query_db("DELETE FROM study_plans WHERE user_id = %s", (user_id,), commit=True)

            display_plans = []
            for day_idx in range(1, plan_days + 1):
                start_pg = (day_idx - 1) * pages_per_day + 1
                end_pg = total_pages if day_idx == plan_days else day_idx * pages_per_day
                title = f"Module {day_idx}: Review Pages {start_pg} to {end_pg}"
                t_date = date.today() + datetime.timedelta(days=day_idx)
                start_time = "18:00:00"
                end_time = f"{min(23, 18 + daily_hours)}:00:00"

                query_db("""
                    INSERT INTO study_plans (user_id, plan_title, scheduled_date, start_time, end_time)
                    VALUES (%s, %s, %s, %s, %s)
                """, (user_id, title, t_date, start_time, end_time), commit=True)

                display_plans.append({
                    "day": day_idx,
                    "date": t_date.strftime("%d %b %Y"),
                    "title": title,
                    "time": f"6:00 PM – {6 + daily_hours}:00 PM"
                })

            return render_template(
                "plan_view.html",
                doc_name=doc["file_name"],
                plans=display_plans,
                days_left=days_left,
                daily_hours=daily_hours
            )

    documents = query_db("SELECT id, file_name FROM documents WHERE user_id = %s", (user_id,))
    return render_template("generate_plan.html", documents=documents or [])

@app.route("/settings", methods=["GET", "POST"])
def settings():
    if "user_id" not in session:
        return redirect("/")

    user_id = session["user_id"]
    message = None
    msg_type = "success"

    if request.method == "POST":
        action = request.form.get("action")
        if action == "update_profile":
            new_name = request.form.get("name", "").strip()
            if new_name:
                query_db("UPDATE users SET name = %s WHERE id = %s", (new_name, user_id), commit=True)
                session["user_name"] = new_name
                message = "Profile name updated successfully!"
            else:
                message = "Name cannot be empty."
                msg_type = "error"
        elif action == "update_password":
            current_pwd = request.form.get("current_password", "")
            new_pwd = request.form.get("new_password", "")
            user_data = query_db("SELECT password FROM users WHERE id = %s", (user_id,), one=True)
            if user_data and check_password_hash(user_data["password"], current_pwd):
                if len(new_pwd) >= 6:
                    hashed = generate_password_hash(new_pwd)
                    query_db("UPDATE users SET password = %s WHERE id = %s", (hashed, user_id), commit=True)
                    message = "Password changed successfully!"
                else:
                    message = "New password must be at least 6 characters."
                    msg_type = "error"
            else:
                message = "Current password does not match."
                msg_type = "error"

    user = query_db("SELECT name, email FROM users WHERE id = %s", (user_id,), one=True)
    return render_template(
        "settings.html",
        user=user,
        message=message,
        msg_type=msg_type,
        engine_mode=session.get("engine_mode", "hybrid"),
        default_hours=session.get("default_study_hours", "2")
    )

if __name__ == "__main__":
    app.run(debug=True)