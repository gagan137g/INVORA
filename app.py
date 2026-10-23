from flask import Flask, render_template, request, redirect, session, jsonify
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
UPLOAD_FOLDER = "uploads"
ALLOWED_EXTENSIONS = {"pdf"}

app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
client = genai.Client(api_key="AQ.Ab8RN6I6fYAlMj7Luv2S5TARw98peJ2FRds3AOrFPSl-Db8YuA")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
# =============================================================
# DATABASE CONNECTION (MySQL with SQLite Cloud Fallback)
# =============================================================
def get_db_connection():
    # If MySQL credentials fail (e.g. running on cloud/Render), fallback to SQLite
    try:
        return mysql.connector.connect(
            host=os.environ.get("DB_HOST", "localhost"),
            user=os.environ.get("DB_USER", "root"),
            password=os.environ.get("DB_PASSWORD", "gagan@2006"),
            database=os.environ.get("DB_NAME", "invora_db")
        )
    except Exception as e:
        # Cloud / Local Fallback: SQLite
        db_path = os.path.join(os.path.dirname(__file__), "invora.db")
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn


def init_db():
    conn = get_db_connection()
    # Only run table creation if using SQLite fallback
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

# Auto-initialize database tables on launch
init_db()

def query_db(query, args=(), one=False, commit=False):
    """
    Executes queries transparently across MySQL (%s) and SQLite (?).
    Returns results as dictionary-accessible rows.
    """
    conn = get_db_connection()
    is_sqlite = isinstance(conn, sqlite3.Connection)
    
    # Auto-convert %s to ? if currently running on SQLite fallback
    if is_sqlite:
        query = query.replace("%s", "?")
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
        # Convert sqlite3.Row objects to standard dictionaries
        rv = [dict(row) for row in rv]

    return (rv[0] if rv else None) if one else rv

# =============================================================
# ---------------------------
# Database & File Helpers
# ---------------------------
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

# ---------------------------
# Stop Words & NLP Utilities
# ---------------------------
STOP_WORDS = {
    "the", "and", "is", "in", "it", "of", "to", "a", "an", "this", "that",
    "these", "those", "with", "as", "for", "on", "by", "at", "from", "be",
    "are", "was", "were", "or", "which", "into", "has", "have", "had", "not",
    "such", "also", "each", "other", "than", "then", "more", "most", "about"
}

# ---------------------------
# AI Primary & Local Fallback (Ollama)
# ---------------------------
def ask_local_backup(prompt):
    """Fallback handler using local Ollama instance (qwen2.5:0.5b). Safe for low-spec setups."""
    try:
        res = requests.post(
            "http://localhost:11434/api/generate",
            json={
                "model": "qwen2.5:0.5b",
                "prompt": prompt,
                "stream": False
            },
            timeout=30
        )
        if res.status_code == 200:
            return res.json().get("response", "").strip()
    except Exception as e:
        print(f"[Local AI Fallback Error]: {e}")
    return None

def get_ai_response(prompt):
    """Generation chain targeting active 3.x models."""
    candidate_models = [
        "gemini-3.1-pro-preview",
        "gemini-3.5-flash-lite",
        "gemini-3.6-flash"
    ]

    for model_name in candidate_models:
        for attempt in range(2):
            try:
                response = client.models.generate_content(
                    model=model_name,
                    contents=prompt
                )
                if response and response.text:
                    return response.text.strip()
            except Exception as e:
                err_str = str(e)
                print(f"[Gemini {model_name} Attempt {attempt + 1} Error]: {err_str}")
                
                if "503" in err_str or "429" in err_str or "unavailable" in err_str.lower():
                    time.sleep(1.5)
                    continue
                else:
                    break

    local_output = ask_local_backup(prompt)
    if local_output:
        return local_output

    return None

# ---------------------------
# Deterministic Offline Generators
# ---------------------------
def generate_offline_summary(text):
    clean_text = re.sub(r'\s+', ' ', text).strip()
    sentences = [s.strip() for s in re.split(r'(?<=[.!?])\s+', clean_text) if len(s.split()) >= 6]
    
    words = re.findall(r'\b[A-Za-z]{3,}\b', clean_text.lower())
    freq = {w: words.count(w) for w in set(words) if w not in STOP_WORDS}
    scored = sorted(sentences, key=lambda s: sum(freq.get(w.lower(), 0) for w in re.findall(r'\b[A-Za-z]{3,}\b', s)), reverse=True)

    overview = " ".join([s for s in scored[:2]]) if len(scored) >= 2 else "Document indexed and structured for academic review."

    def_pattern = r'([A-Z][A-Za-z\s]{2,20})\s+(?:is defined as|refers to|is a|means|acts as)\s+([^.!?]+[.!?])'
    concepts = []
    for s in sentences:
        m = re.search(def_pattern, s)
        if m and len(concepts) < 4:
            concepts.append(f"• {m.group(1).strip()}: {m.group(2).strip()}")
    if not concepts:
        concepts = [f"• {s}" for s in scored[2:6]] if len(scored) >= 6 else [f"• Core Theory: {overview}"]

    mechanisms = [f"• {s}" for s in scored[6:9]] if len(scored) >= 9 else [f"• Key Workflow: Review core chapter definitions and module procedures."]
    takeaways = [f"• {s}" for s in scored[9:12]] if len(scored) >= 12 else [f"• High-Yield Focus: Review definitions, system architectures, and primary test cases."]

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

# ---------------------------
# Hybrid Synthesizer
# ---------------------------
def generate_study_summary(text):
    if session.get("engine_mode") == "offline":
        return generate_offline_summary(text)

    summary_prompt = f"""You are INVORA, an expert university-level academic study assistant.
Generate a structured, comprehensive study summary from the provided document text.

Provide substantial, informative content under each of the following exact sections.
Do NOT use Markdown asterisks (** or *). Use clean, plain text and bullet points (•).

OVERVIEW:
Write a well-developed paragraph (4 to 5 sentences) explaining the main subject, background context, core objectives, and why this topic is important.

KEY CONCEPTS:
- List 4 to 5 fundamental terms, definitions, or principles found in the material.
- Provide a clear, thorough explanation for each concept explaining what it is and how it works.

IMPORTANT MECHANISMS & WORKFLOWS:
- Break down the core processes, technical workflows, rules, or algorithms described in the document.
- Explain the step-by-step logic or interactions between components in detail.

KEY TAKEAWAYS & EXAM FOCUS:
- Highlight 3 to 4 critical points, comparison distinctions, or high-probability exam topics students need to remember.

Source Document:
{text[:7000]}"""

    ai_result = get_ai_response(summary_prompt)
    if ai_result:
        return ai_result
    return generate_offline_summary(text)

def generate_study_notes(text):
    if session.get("engine_mode") == "offline":
        return generate_offline_notes(text)

    notes_prompt = f"""You are INVORA, an expert university-level academic study assistant.
Generate deep, exhaustive lecture study notes from this material for thorough exam revision.
Do NOT use markdown asterisks (** or *). Plain text and bullets (•) only.

1. THEORETICAL ARCHITECTURE & FOUNDATIONS
- Deep dive into principles, structures, and theoretical models.

2. STEP-BY-STEP TECHNICAL WORKFLOWS
- Detailed descriptions of operational procedures and algorithms.

3. EXHAUSTIVE TERMINOLOGY GLOSSARY
- Comprehensive list of all critical formulas, definitions, and technical terms.

Source Document:
{text[:7000]}"""

    ai_result = get_ai_response(notes_prompt)
    if ai_result:
        return ai_result
    return generate_offline_notes(text)

# -------------------------------------------------------------
# Clean Sequential Chapter Extractor (Guaranteed Unique Text)
# -------------------------------------------------------------
def get_pdf_chapters_and_text(pdf_path):
    """
    Extracts all chapters from the textbook and ensures each chapter
    gets its own distinct, non-overlapping slice of pages.
    """
    full_text = ""
    extracted_pages = []

    try:
        reader = PdfReader(pdf_path)
        total_pages = len(reader.pages)

        # 1. Read all pages
        for p_idx in range(total_pages):
            try:
                p_text = reader.pages[p_idx].extract_text() or ""
                extracted_pages.append(p_text)
                full_text += p_text + "\n"
            except Exception:
                extracted_pages.append("")

        # 2. Check native outlines / bookmarks
        def walk_outline(outline_node):
            entries = []
            for item in outline_node:
                if isinstance(item, list):
                    entries.extend(walk_outline(item))
                elif isinstance(item, dict):
                    t = item.get("/Title", "")
                    if t:
                        entries.append(str(t).strip())
                elif hasattr(item, "title"):
                    entries.append(str(item.title).strip())
            return entries

        outline_titles = []
        try:
            if reader.outline:
                outline_titles = walk_outline(reader.outline)
        except Exception:
            outline_titles = []

        ch_regex = re.compile(r'^(chapter|unit|module|\d+[\.\s])', re.IGNORECASE)
        valid_bms = [b.strip() for b in outline_titles if ch_regex.match(b.strip())]

        # Determine total chapter count (e.g. 18)
        num_chapters = len(valid_bms) if len(valid_bms) >= 3 else 18

        # 3. Partition pages evenly so NO TWO CHAPTERS SHARE THE SAME TEXT
        pages_per_ch = max(1, len(extracted_pages) // num_chapters)
        formatted_chapters = []

        for idx in range(num_chapters):
            start_p = idx * pages_per_ch
            end_p = len(extracted_pages) if idx == num_chapters - 1 else (idx + 1) * pages_per_ch
            
            # Distinct slice of pages for this specific chapter
            chapter_pages = extracted_pages[start_p:end_p]
            slice_text = "\n".join([p for p in chapter_pages if p.strip()])

            # If this section has very sparse text, use its specific page range with fallback
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
        # Default safety: 18 distinct partitioned chapters
        return [
            {"title": f"Chapter {i+1}", "text": f"Chapter {i+1} curriculum review.", "chapter_num": i+1}
            for i in range(18)
        ], full_text

# -------------------------------------------------------------
# Uncapped Academic MCQ Engine (Unique Questions Per Chapter)
# -------------------------------------------------------------
def generate_academic_mcqs(text, chapter_title="Chapter 1"):
    """
    Extracts distinct questions strictly from the selected chapter's text.
    Uses seeded randomization to ensure Chapter 1, Chapter 2, etc. have completely
    unique, non-overlapping question sets.
    """
    # Deterministic local RNG seeded by chapter name so it is consistent yet unique per chapter
    rng = random.Random(hash(chapter_title))

    clean = re.sub(r'\s+', ' ', text)
    sentences = [s.strip() for s in re.split(r'(?<=[.!?])\s+', clean) if 20 < len(s.strip()) < 220]

    # Collect unique technical keywords specifically from THIS chapter's text
    potential_terms = re.findall(r'\b[A-Z][a-zA-Z0-9_\-]{2,}(?:\s+[A-Z][a-zA-Z0-9_\-]+){0,2}\b', clean)
    filtered_terms = list(dict.fromkeys([
        t.strip() for t in potential_terms 
        if t.lower() not in STOP_WORDS and len(t.strip()) > 3 and not t.isdigit()
    ]))

    # Subject distractor bank
    distractor_pool = [
        "Symmetric Encryption", "Public Key Infrastructure", "Hash Digest", "Diffie-Hellman Key Exchange",
        "RSA Cryptosystem", "Elliptic Curve Cryptography", "Digital Signature Standard",
        "Message Authentication Code", "Advanced Encryption Standard (AES)", "Data Encryption Standard (DES)",
        "Block Cipher Mode", "Initialization Vector", "Cryptanalytic Attack", "Non-Repudiation Service",
        "Stream Cipher Keystream", "Modular Exponentiation", "Euler Totient Function", "Linear Feedback Shift Register",
        "Certificate Authority", "Transport Layer Security", "Virtual Memory Page Table", "Round Robin Scheduling"
    ]
    term_universe = list(set(filtered_terms + distractor_pool))

    questions_pool = []
    seen_questions = set()

    # Pattern 1: Formal Definitions inside this chapter
    def_regex = re.compile(
        r'\b([A-Z][a-zA-Z0-9_\-\s]{2,30})\b\s+(is defined as|refers to|is known as|can be described as|is called|means)\s+([^,.;]{15,120})',
        re.IGNORECASE
    )

    # Pattern 2: Functional Actions inside this chapter
    func_regex = re.compile(
        r'\b([A-Z][a-zA-Z0-9_\-\s]{2,30})\b\s+(is used to|is designed to|serves to|functions to|is utilized for|helps in)\s+([^,.;]{15,120})',
        re.IGNORECASE
    )

    # 1. Parse definitions in this chapter
    for s in sentences:
        m_def = def_regex.search(s)
        if m_def:
            term = m_def.group(1).strip()
            desc = m_def.group(3).strip()
            if 1 <= len(term.split()) <= 4:
                wrong_choices = [t for t in term_universe if t.lower() != term.lower()]
                if len(wrong_choices) >= 3:
                    q_text = f"In {chapter_title}, which concept is defined as: \"{desc}\"?"
                    if q_text not in seen_questions:
                        seen_questions.add(q_text)
                        opts = rng.sample(wrong_choices, 3) + [term]
                        rng.shuffle(opts)
                        questions_pool.append({
                            "question": q_text,
                            "options": opts,
                            "answer": term
                        })

    # 2. Parse mechanisms in this chapter
    for s in sentences:
        m_func = func_regex.search(s)
        if m_func:
            term = m_func.group(1).strip()
            action = m_func.group(3).strip()
            if 1 <= len(term.split()) <= 4:
                wrong_choices = [t for t in term_universe if t.lower() != term.lower()]
                if len(wrong_choices) >= 3:
                    q_text = f"According to {chapter_title}, what is primarily used to {action}?"
                    if q_text not in seen_questions:
                        seen_questions.add(q_text)
                        opts = rng.sample(wrong_choices, 3) + [term]
                        rng.shuffle(opts)
                        questions_pool.append({
                            "question": q_text,
                            "options": opts,
                            "answer": term
                        })

    # 3. Key Term Cloze questions from this chapter's sentences
    for s in sentences:
        for term in filtered_terms[:25]:
            if re.search(r'\b' + re.escape(term) + r'\b', s) and len(term.split()) <= 2:
                wrong_choices = [t for t in term_universe if t.lower() != term.lower()]
                if len(wrong_choices) >= 3:
                    blanked = re.sub(r'\b' + re.escape(term) + r'\b', '[ _____ ]', s, count=1)
                    q_text = f"From {chapter_title}, complete the following technical statement:\n\"{blanked}\""
                    if q_text not in seen_questions:
                        seen_questions.add(q_text)
                        opts = rng.sample(wrong_choices, 3) + [term]
                        rng.shuffle(opts)
                        questions_pool.append({
                            "question": q_text,
                            "options": opts,
                            "answer": term
                        })
                        break

    # 4. Varied Chapter-Specific Bank (Rotated based on chapter number to prevent overlap)
    # Extracts chapter number (e.g., 2 from 'Chapter 2')
    ch_num_match = re.search(r'\d+', chapter_title)
    ch_num = int(ch_num_match.group()) if ch_num_match else 1

    chapter_banks = {
        1: [
            {"q": "Which security service ensures that data is not disclosed to unauthorized individuals?", "opts": ["Confidentiality", "Integrity", "Availability", "Authentication"], "ans": "Confidentiality"},
            {"q": "What type of attack involves unauthorized interception and observation of transmission contents?", "opts": ["Passive Attack (Eavesdropping)", "Active Attack (Modification)", "Denial of Service", "Replay Attack"], "ans": "Passive Attack (Eavesdropping)"},
            {"q": "Which security model element prevents the sender from denying having sent a message?", "opts": ["Non-repudiation", "Integrity", "Access Control", "Availability"], "ans": "Non-repudiation"}
        ],
        2: [
            {"q": "In modular arithmetic, what is the result of 23 mod 7?", "opts": ["2", "3", "5", "1"], "ans": "2"},
            {"q": "Which algorithm is used to find the greatest common divisor (GCD) of two integers?", "opts": ["Euclidean Algorithm", "Dijkstra's Algorithm", "Miller-Rabin Test", "Fermat's Theorem"], "ans": "Euclidean Algorithm"},
            {"q": "Two numbers 'a' and 'b' are relatively prime (coprime) if their GCD is equal to:", "opts": ["1", "0", "Prime number p", "Infinite"], "ans": "1"}
        ],
        3: [
            {"q": "Which of the following is an example of a traditional monoalphabetic substitution cipher?", "opts": ["Caesar Cipher", "Playfair Cipher", "Vigenère Cipher", "Hill Cipher"], "ans": "Caesar Cipher"},
            {"q": "What cryptanalytic technique counts letter occurrences to break substitution ciphers?", "opts": ["Frequency Analysis", "Linear Cryptanalysis", "Differential Cryptanalysis", "Birthday Attack"], "ans": "Frequency Analysis"},
            {"q": "In a transposition cipher, how is ciphertext generated from plaintext?", "opts": ["By rearranging the order of characters", "By replacing letters with numbers", "By applying XOR logic with an IV", "By computing a cryptographic hash"], "ans": "By rearranging the order of characters"}
        ],
        4: [
            {"q": "What is the primary difference between a stream cipher and a block cipher?", "opts": ["Stream ciphers encrypt bit-by-bit or byte-by-byte; block ciphers encrypt chunks", "Stream ciphers only use asymmetric keys", "Block ciphers cannot be used for network transmission", "Stream ciphers require 128-bit blocks"], "ans": "Stream ciphers encrypt bit-by-bit or byte-by-byte; block ciphers encrypt chunks"},
            {"q": "Which design principle in modern ciphers spreads the influence of one plaintext symbol over many ciphertext symbols?", "opts": ["Diffusion", "Confusion", "Permutation", "Substitution"], "ans": "Diffusion"}
        ],
        5: [
            {"q": "What is the standard block size and key size for the Data Encryption Standard (DES)?", "opts": ["64-bit block size with 56-bit effective key", "128-bit block size with 128-bit key", "64-bit block size with 64-bit effective key", "512-bit block size with 256-bit key"], "ans": "64-bit block size with 56-bit effective key"},
            {"q": "How many rounds of permutation and substitution are performed in standard DES?", "opts": ["16 rounds", "10 rounds", "12 rounds", "32 rounds"], "ans": "16 rounds"}
        ],
        6: [
            {"q": "What structure is used by the Advanced Encryption Standard (AES)?", "opts": ["Substitution-Permutation Network (SPN)", "Feistel Cipher Structure", "Linear Shift Feedback Matrix", "Transposition Lattice"], "ans": "Substitution-Permutation Network (SPN)"},
            {"q": "How many rounds does AES use for a 128-bit key?", "opts": ["10 rounds", "12 rounds", "14 rounds", "16 rounds"], "ans": "10 rounds"}
        ],
        7: [
            {"q": "Which block cipher mode of operation introduces an Initialization Vector (IV) and chains blocks together?", "opts": ["Cipher Block Chaining (CBC)", "Electronic Codebook (ECB)", "Counter (CTR) Mode", "Output Feedback (OFB)"], "ans": "Cipher Block Chaining (CBC)"},
            {"q": "Why is Electronic Codebook (ECB) mode considered insecure for encrypting structured files?", "opts": ["Identical plaintext blocks always produce identical ciphertext blocks", "It requires asymmetric key pairs", "It operates too slowly for network streams", "It has no decryption formula"], "ans": "Identical plaintext blocks always produce identical ciphertext blocks"}
        ],
        8: [
            {"q": "Which mathematical problem provides the basis for the security of RSA cryptography?", "opts": ["Integer Factorization of large primes", "Discrete Logarithm on elliptic curves", "Solving shortest vector problems", "Knapsack NP-complete problem"], "ans": "Integer Factorization of large primes"},
            {"q": "In asymmetric cryptography, which key is used by the sender to encrypt a confidential message for Bob?", "opts": ["Bob's Public Key", "Bob's Private Key", "Sender's Private Key", "Certificate Authority Key"], "ans": "Bob's Public Key"}
        ],
        9: [
            {"q": "What is the primary security goal of a cryptographic hash function like SHA-256?", "opts": ["Data Integrity verification", "Confidential bulk encryption", "Two-way data compression", "Key distribution"], "ans": "Data Integrity verification"},
            {"q": "What property ensures that it is computationally infeasible to find any two distinct inputs that produce the same hash value?", "opts": ["Collision Resistance", "Pre-image Resistance", "Avalanche Effect", "Confusion"], "ans": "Collision Resistance"}
        ],
        10: [
            {"q": "A Digital Signature provides which of the following essential security requirements?", "opts": ["Authentication, Integrity, and Non-repudiation", "High data compression and encryption", "Symmetric key exchange", "Hardware-level clock synchronization"], "ans": "Authentication, Integrity, and Non-repudiation"},
            {"q": "Which key is used to generate a digital signature on a document?", "opts": ["Signer's Private Key", "Signer's Public Key", "Receiver's Public Key", "CA Root Key"], "ans": "Signer's Private Key"}
        ]
    }

    # Add chapter-specific bank questions if available
    specific_qs = chapter_banks.get(ch_num, [])
    for sq in specific_qs:
        if sq["q"] not in seen_questions:
            seen_questions.add(sq["q"])
            opts = list(sq["opts"])
            rng.shuffle(opts)
            questions_pool.append({
                "question": sq["q"],
                "options": opts,
                "answer": sq["ans"]
            })

    # If the text was short, add rotated conceptual questions unique to this chapter number
    if len(questions_pool) < 5:
        generic_curriculum = [
            {"q": f"What is the foundational architectural goal studied in {chapter_title}?", "ans": "Understanding the mechanisms and mathematical principles for that module"},
            {"q": f"Which parameter represents the primary design constraint evaluated in {chapter_title}?", "ans": "Computational overhead vs security strength"},
            {"q": f"In {chapter_title}, how are boundary edge cases systematically tested?", "ans": "By validating through rigorous cryptographic test vectors"},
            {"q": f"What is the risk of protocol failure in systems implementing {chapter_title}?", "ans": "Vulnerability to side-channel and replay attacks"},
            {"q": f"Which standard organization publishes specifications governing {chapter_title}?", "ans": "NIST / IETF"}
        ]
        for gq in generic_curriculum:
            if gq["q"] not in seen_questions:
                seen_questions.add(gq["q"])
                distractors = rng.sample([t for t in term_universe if t != gq["ans"]], 3)
                opts = distractors + [gq["ans"]]
                rng.shuffle(opts)
                questions_pool.append({
                    "question": gq["q"],
                    "options": opts,
                    "answer": gq["ans"]
                })

    rng.shuffle(questions_pool)
    return questions_pool    
# ---------------------------
# Auth: Login, Register, Forgot Password
# ---------------------------
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

        db = get_db_connection()
        is_sqlite = isinstance(db, sqlite3.Connection)

        if is_sqlite:
            cursor = db.cursor()
            cursor.execute("SELECT * FROM users WHERE email=?", (email,))
        else:
            cursor = db.cursor(dictionary=True)
            cursor.execute("SELECT * FROM users WHERE email=?", (email,))

        user = cursor.fetchone()
        cursor.close()
        db.close()

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
        hashed_password = generate_password_hash(password)

        db = get_db_connection()
        cursor = db.cursor()
        try:
            cursor.execute(
                "INSERT INTO users (name, email, password) VALUES (?, ?, ?)",
                (name, email, hashed_password)
            )
            db.commit()
        except mysql.connector.Error as error:
            db.rollback()
            cursor.close()
            db.close()
            return f"Registration failed: {error}"

        cursor.close()
        db.close()
        return redirect("/")

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
            db = get_db_connection()
            cursor = db.cursor(dictionary=True)
            cursor.execute("SELECT id FROM users WHERE email = %s", (email,))
            user = cursor.fetchone()

            if user:
                hashed_pw = generate_password_hash(new_password)
                cursor.execute("UPDATE users SET password = %s WHERE id = %s", (hashed_pw, user["id"]))
                db.commit()
                cursor.close()
                db.close()
                success = "Password reset successfully! You can now log in."
            else:
                cursor.close()
                db.close()
                error = "No account found with this email address."

    return render_template("forgot_password.html", error=error, success=success)

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")

# ---------------------------
# Dynamic Dashboard
# ---------------------------
@app.route("/dashboard")
def dashboard():
    if "user_id" not in session:
        return redirect("/")

    user_id = session["user_id"]
    db = get_db_connection()
    cursor = db.cursor(dictionary=True)

    cursor.execute("SELECT COUNT(*) AS total FROM documents WHERE user_id = %s", (user_id,))
    total_pdfs = cursor.fetchone()["total"]

    cursor.execute("""
        SELECT COUNT(*) AS total_quizzes, 
               COALESCE(ROUND(AVG(score_percentage)), 0) AS avg_score 
        FROM quiz_results 
        WHERE user_id = %s
    """, (user_id,))
    quiz_stats = cursor.fetchone()
    total_quizzes = quiz_stats["total_quizzes"] if quiz_stats else 0
    avg_score = quiz_stats["avg_score"] if quiz_stats else 0

    pdf_progress = min(100, int((total_pdfs / 10) * 100)) if total_pdfs > 0 else 0
    quiz_progress = min(100, int((total_quizzes / 10) * 100)) if total_quizzes > 0 else 0
    overall_progress = int((pdf_progress * 0.5) + (quiz_progress * 0.5))

    cursor.execute("""
        SELECT id, file_name, 
               DATE_FORMAT(uploaded_at, '%d %b %Y') AS upload_date, 
               page_count 
        FROM documents 
        WHERE user_id = %s 
        ORDER BY uploaded_at DESC 
        LIMIT 4
    """, (user_id,))
    recent_pdfs = cursor.fetchall()

    cursor.execute("""
        SELECT plan_title, 
               DATE_FORMAT(scheduled_date, '%d') AS day_num,
               TIME_FORMAT(start_time, '%h:%i %p') AS start_t,
               TIME_FORMAT(end_time, '%h:%i %p') AS end_t
        FROM study_plans 
        WHERE user_id = %s AND scheduled_date >= CURDATE()
        ORDER BY scheduled_date ASC, start_time ASC 
        LIMIT 3
    """, (user_id,))
    upcoming_plans = cursor.fetchall()

    cursor.close()
    db.close()

    return render_template(
        "dashboard.html",
        username=session["user_name"],
        total_pdfs=total_pdfs,
        total_quizzes=total_quizzes,
        avg_score=avg_score,
        study_hours=round((total_pdfs * 1.5) + (total_quizzes * 0.4), 1),
        pdf_progress=pdf_progress,
        quiz_progress=quiz_progress,
        overall_progress=overall_progress,
        recent_pdfs=recent_pdfs,
        upcoming_plans=upcoming_plans
    )

# ---------------------------
# Upload & Hybrid Summarizer
# ---------------------------
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
        
        pages_to_read = []
        if total_pages <= 8:
            pages_to_read = list(range(total_pages))
        else:
            pages_to_read = [0, 1, 2, total_pages // 4, total_pages // 2, (3 * total_pages) // 4, total_pages - 2, total_pages - 1]

        extracted_chunks = []
        for idx in pages_to_read:
            if idx < total_pages:
                txt = reader.pages[idx].extract_text()
                if txt:
                    clean_page = " ".join(txt.split())
                    if len(clean_page) > 40:
                        extracted_chunks.append(clean_page)

        combined_text = "\n\n".join(extracted_chunks)

        summary_result = generate_study_summary(combined_text)
        notes_result = generate_study_notes(combined_text)

        db = get_db_connection()
        cursor = db.cursor()
        cursor.execute("""
            INSERT INTO documents (user_id, file_name, page_count, summary_text, notes_text)
            VALUES (%s, %s, %s, %s, %s)
        """, (session["user_id"], filename, total_pages, summary_result, notes_result))
        db.commit()
        cursor.close()
        db.close()

        return render_template("upload.html", summary=summary_result, notes=notes_result)

    return render_template("upload.html")

# ---------------------------
# Notes & Summaries Repository View
# ---------------------------
@app.route("/notes")
def notes_hub():
    if "user_id" not in session:
        return redirect("/")

    user_id = session["user_id"]
    db = get_db_connection()
    cursor = db.cursor(dictionary=True)
    cursor.execute("""
        SELECT id, file_name, 
               DATE_FORMAT(uploaded_at, '%d %b %Y') AS upload_date, 
               page_count 
        FROM documents 
        WHERE user_id = %s 
        ORDER BY uploaded_at DESC
    """, (user_id,))
    docs = cursor.fetchall()
    cursor.close()
    db.close()

    return render_template("notes_hub.html", docs=docs)

@app.route("/document/<int:doc_id>")
def view_document_notes(doc_id):
    if "user_id" not in session:
        return redirect("/")

    user_id = session["user_id"]
    db = get_db_connection()
    cursor = db.cursor(dictionary=True)
    cursor.execute("""
        SELECT id, file_name, summary_text, notes_text, page_count,
               DATE_FORMAT(uploaded_at, '%d %b %Y') AS upload_date
        FROM documents 
        WHERE id = %s AND user_id = %s
    """, (doc_id, user_id))
    doc = cursor.fetchone()
    cursor.close()
    db.close()

    if not doc:
        return redirect("/dashboard")

    return render_template("document_view.html", doc=doc)

# ---------------------------
# AI Chat
# ---------------------------
@app.route("/chat", methods=["GET", "POST"])
def chat():
    if "user_id" not in session:
        return redirect("/")

    user_id = session["user_id"]
    db = get_db_connection()
    cursor = db.cursor(dictionary=True)

    if request.method == "POST":
        data = request.get_json() if request.is_json else request.form
        question = data.get("question", "").strip()
        doc_id = data.get("document_id")
        history = data.get("history", [])

        if not question:
            cursor.close()
            db.close()
            return jsonify({"status": "error", "message": "Question cannot be empty."}), 400

        doc_context = ""
        doc_name = "General Knowledge"

        if doc_id and str(doc_id).isdigit() and int(doc_id) > 0:
            cursor.execute("SELECT file_name FROM documents WHERE id = %s AND user_id = %s", (doc_id, user_id))
            doc_record = cursor.fetchone()
            if doc_record:
                doc_name = doc_record["file_name"]
                filepath = os.path.join(app.config["UPLOAD_FOLDER"], doc_name)
                if os.path.exists(filepath):
                    raw_text, _ = extract_text_and_pages(filepath)
                    doc_context = raw_text[:6000]

        cursor.close()
        db.close()

        history_text = ""
        if history and isinstance(history, list):
            history_snippets = []
            for item in history[-6:]:
                role = "Student" if item.get("role") == "user" else "INVORA"
                text = item.get("text", "").strip()
                if text:
                    history_snippets.append(f"{role}: {text}")
            if history_snippets:
                history_text = "\n".join(history_snippets)

        if doc_context:
            prompt = f"""You are INVORA, an intelligent, helpful, and friendly academic learning assistant.
Maintain continuous conversational context with the student. If they ask follow-up questions, resolve pronouns or references ('it', 'that', 'the second point', 'explain more') using the conversation history below.

The student has linked a document: "{doc_name}".
RECENT CONVERSATION HISTORY:
{history_text if history_text else "No previous conversation."}

DOCUMENT EXCERPT:
{doc_context}

STUDENT MESSAGE:
{question}

INSTRUCTIONS:
1. If the message is a greeting or natural follow-up to prior messages, reply smoothly and contextually.
2. If the user's question relates to the document, use the provided Document Excerpt accurately.
3. If the user asks something beyond the excerpt or general knowledge, answer helpfully using your broad knowledge base.
4. Keep the response readable: use clean plain text with bullet points (•) when breaking down technical concepts. Do NOT use markdown bolding (** or *)."""
        else:
            prompt = f"""You are INVORA, an intelligent, helpful, and friendly personal learning assistant.
Maintain continuous conversational context with the student. If they ask follow-up questions, resolve pronouns or references ('it', 'that', 'the second point', 'explain more') using the conversation history below.

RECENT CONVERSATION HISTORY:
{history_text if history_text else "No previous conversation."}

STUDENT MESSAGE:
{question}

INSTRUCTIONS:
1. Handle continuous casual conversations, motivation, study tips, and deep academic explanations.
2. If the user greets you or makes small talk, be warm, supportive, and conversational.
3. If the user asks a complex academic question or follow-up, provide a structured explanation using clean plain text and bullet points (•).
4. Do NOT use markdown bolding (** or *)."""

        answer = get_ai_response(prompt)
        if not answer:
            answer = "Hello! I am currently running in offline backup mode. How can I help you with your studies today?"

        return jsonify({
            "status": "success",
            "question": question,
            "answer": answer,
            "doc_name": doc_name
        })

    cursor.execute("SELECT id, file_name FROM documents WHERE user_id = %s ORDER BY uploaded_at DESC", (user_id,))
    user_documents = cursor.fetchall()
    cursor.close()
    db.close()

    return render_template("chat.html", documents=user_documents)

# ---------------------------
# Integrated Chapter & Full-Doc Quiz (Uncapped MCQs)
# ---------------------------
@app.route("/quiz", methods=["GET", "POST"])
def quiz():
    if "user_id" not in session:
        return redirect("/")

    user_id = session["user_id"]
    db = get_db_connection()
    cursor = db.cursor(dictionary=True)

    cursor.execute("SELECT id, file_name FROM documents WHERE user_id = %s ORDER BY uploaded_at DESC", (user_id,))
    all_docs = cursor.fetchall()

    if not all_docs:
        cursor.close()
        db.close()
        return redirect("/dashboard")

    # Determine current document
    doc_id = request.args.get("doc_id", type=int) or request.form.get("doc_id", type=int)
    selected_doc = next((d for d in all_docs if d["id"] == doc_id), all_docs[0])

    # 1. Handle Final Assessment Submission (Scoring & DB Record)
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

        try:
            cursor.execute(
                "INSERT INTO quiz_results (user_id, document_id, score, total_questions, score_percentage) VALUES (%s, %s, %s, %s, %s)",
                (user_id, selected_doc["id"], score, total_q, int(percentage))
            )
            db.commit()
        except Exception as err:
            print("[QUIZ RESULT DB ERROR]:", err)

        cursor.close()
        db.close()

        return render_template(
            "quiz_results.html",
            score=score,
            total=total_q,
            percentage=percentage,
            review=review_data,
            doc_name=selected_doc["file_name"]
        )

    cursor.close()
    db.close()

    # 2. Extract Native Chapters from the Uploaded PDF File
    filepath = os.path.join(app.config["UPLOAD_FOLDER"], selected_doc["file_name"])
    chapters = []
    raw_text = ""

    if os.path.exists(filepath):
        chapters, raw_text = get_pdf_chapters_and_text(filepath)

    # 100% Boundary Protection: Ensure chapters is never empty
    if not isinstance(chapters, list) or len(chapters) == 0:
        chapters = [{"title": "Chapter 1: Syllabus Overview & Core Topics", "text": raw_text}]

    # 3. Determine Selected Scope/Chapter with Strict Safe Clamping
    chapter_choice = request.form.get("chapter_index") or request.args.get("chapter_index", "0")
    selected_chapter_idx = 0

    if str(chapter_choice).strip() != "all" and str(chapter_choice).strip().isdigit():
        req_idx = int(chapter_choice)
        if 0 <= req_idx < len(chapters):
            selected_chapter_idx = req_idx
        else:
            selected_chapter_idx = 0

        target_slice = chapters[selected_chapter_idx]
        scope_title = target_slice.get("title", f"Chapter {selected_chapter_idx + 1}")
        target_text = target_slice.get("text", raw_text)
    else:
        scope_title = "Entire Document"
        target_text = raw_text

    # 4. Generate ALL possible MCQs for this chapter (uncapped)
    questions = generate_academic_mcqs(target_text, chapter_title=scope_title)

    return render_template(
        "quiz.html",
        all_docs=all_docs,
        current_doc=selected_doc,
        doc_name=selected_doc["file_name"],
        chapters=chapters,
        selected_chapter_idx=selected_chapter_idx,
        questions=questions,
        scope_label=scope_title
    )

# ---------------------------
# Chapter-wise Modular Roadmap
# ---------------------------
@app.route("/chapter-plan")
@app.route("/chapter-plan/<int:doc_id>")
def chapter_plan(doc_id=None):
    if "user_id" not in session:
        return redirect("/")

    user_id = session["user_id"]
    db = get_db_connection()
    cursor = db.cursor(dictionary=True)

    cursor.execute("SELECT id, file_name FROM documents WHERE user_id = %s ORDER BY uploaded_at DESC", (user_id,))
    all_docs = cursor.fetchall()

    if not all_docs:
        cursor.close()
        db.close()
        return redirect("/dashboard")

    selected_doc = None
    if doc_id:
        selected_doc = next((d for d in all_docs if d["id"] == doc_id), None)
    if not selected_doc:
        selected_doc = all_docs[0]

    cursor.close()
    db.close()

    filepath = os.path.join(app.config["UPLOAD_FOLDER"], selected_doc["file_name"])
    chapters = []
    raw_text = ""

    if os.path.exists(filepath):
        chapters, raw_text = get_pdf_chapters_and_text(filepath)

    if not chapters:
        chapters = [{"title": "Chapter 1", "text": raw_text}]

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

# ---------------------------
# Video Recommendations Module
# ---------------------------
def get_video_recommendations(doc_title, raw_text=""):
    clean_title = doc_title.lower().replace(".pdf", "").replace("_", " ").replace("-", " ")
    topic_keywords = []

    if "daa" in clean_title or "algorithm" in clean_title:
        topic_keywords = [
            {"title": "Design & Analysis of Algorithms Full Course", "query": "DAA full course gate smashers", "channel": "Gate Smashers"},
            {"title": "Asymptotic Notations & Master Theorem", "query": "Master Theorem Abdul Bari", "channel": "Abdul Bari"},
            {"title": "Divide and Conquer: Merge & Quick Sort", "query": "Divide and conquer algorithms Abdul Bari", "channel": "Abdul Bari"},
            {"title": "Dynamic Programming Principles", "query": "Dynamic Programming Introduction Gate Smashers", "channel": "Gate Smashers"},
            {"title": "Greedy Method vs Dynamic Programming", "query": "Greedy Method Abdul Bari", "channel": "Abdul Bari"}
        ]
    elif "crypto" in clean_title or "security" in clean_title or "forouzan" in clean_title:
        topic_keywords = [
            {"title": "Cryptography and Network Security Complete Course", "query": "Cryptography and Network Security Gate Smashers", "channel": "Gate Smashers"},
            {"title": "Symmetric vs Asymmetric Key Cryptography", "query": "Symmetric Asymmetric Cryptography Neso Academy", "channel": "Neso Academy"},
            {"title": "RSA Algorithm Numerical with Example", "query": "RSA Algorithm Abdul Bari", "channel": "Abdul Bari"},
            {"title": "Digital Signatures & Hash Functions (SHA/MD5)", "query": "Digital Signature Gate Smashers", "channel": "Gate Smashers"},
            {"title": "Firewalls and Intrusion Detection Systems", "query": "Firewall types Gate Smashers", "channel": "Gate Smashers"}
        ]
    elif "os" in clean_title or "operating system" in clean_title:
        topic_keywords = [
            {"title": "Operating Systems Complete Playlist", "query": "Operating Systems Gate Smashers", "channel": "Gate Smashers"},
            {"title": "CPU Scheduling Algorithms (FCFS, SJF, RR)", "query": "CPU scheduling algorithms Neso Academy", "channel": "Neso Academy"},
            {"title": "Process Synchronization & Semaphores", "query": "Process Synchronization Gate Smashers", "channel": "Gate Smashers"},
            {"title": "Deadlock Handling & Banker's Algorithm", "query": "Deadlock Bankers Algorithm Abdul Bari", "channel": "Abdul Bari"},
            {"title": "Virtual Memory & Paging Mechanisms", "query": "Paging and Virtual Memory Gate Smashers", "channel": "Gate Smashers"}
        ]
    elif "dbms" in clean_title or "database" in clean_title:
        topic_keywords = [
            {"title": "Database Management Systems (DBMS) Course", "query": "DBMS complete playlist Gate Smashers", "channel": "Gate Smashers"},
            {"title": "Normalization (1NF, 2NF, 3NF, BCNF)", "query": "DBMS Normalization Gate Smashers", "channel": "Gate Smashers"},
            {"title": "SQL Queries & Relational Algebra", "query": "SQL complete tutorial freeCodeCamp", "channel": "freeCodeCamp"},
            {"title": "Transactions and Concurrency Control", "query": "ACID properties Gate Smashers", "channel": "Gate Smashers"}
        ]
    elif "cn" in clean_title or "network" in clean_title:
        topic_keywords = [
            {"title": "Computer Networks Complete Course", "query": "Computer Networks Gate Smashers", "channel": "Gate Smashers"},
            {"title": "OSI vs TCP/IP Model Explained", "query": "OSI Model 7 Layers Neso Academy", "channel": "Neso Academy"},
            {"title": "IP Addressing and Subnetting", "query": "Subnetting made easy NetworkChuck", "channel": "NetworkChuck"}
        ]
    else:
        topic_keywords = [
            {"title": f"Complete Guide to {clean_title.title()}", "query": f"{clean_title} full course tutorial", "channel": "Top Academic Sources"},
            {"title": f"Core Concepts & Problem Solving in {clean_title.title()}", "query": f"{clean_title} concepts explained", "channel": "University Prep"},
            {"title": f"Exam Revision: {clean_title.title()}", "query": f"{clean_title} exam revision one shot", "channel": "Exam Prep Hub"}
        ]

    cards = []
    for item in topic_keywords:
        encoded_q = urllib.parse.quote(item["query"])
        cards.append({
            "topic": item["title"],
            "channel": item["channel"],
            "search_url": f"https://www.youtube.com/results?search_query={encoded_q}",
            "tag": "Video Lecture"
        })
    return cards

@app.route("/videos/<int:doc_id>")
def recommended_videos(doc_id):
    if "user_id" not in session:
        return redirect("/")

    user_id = session["user_id"]
    db = get_db_connection()
    cursor = db.cursor(dictionary=True)

    cursor.execute("SELECT id, file_name FROM documents WHERE id = %s AND user_id = %s", (doc_id, user_id))
    doc = cursor.fetchone()

    cursor.execute("SELECT id, file_name FROM documents WHERE user_id = %s ORDER BY uploaded_at DESC", (user_id,))
    all_docs = cursor.fetchall()

    cursor.close()
    db.close()

    if not doc:
        return redirect("/dashboard")

    recommendations = get_video_recommendations(doc["file_name"])

    return render_template(
        "videos.html",
        doc_name=doc["file_name"],
        current_doc_id=doc["id"],
        videos=recommendations,
        all_docs=all_docs,
        username=session.get("user_name", "Student")
    )

# ---------------------------
# Smart Syllabus Planner
# ---------------------------
@app.route("/generate-plan", methods=["GET", "POST"])
def generate_plan():
    if "user_id" not in session:
        return redirect("/")

    user_id = session["user_id"]
    db = get_db_connection()
    cursor = db.cursor(dictionary=True)

    if request.method == "POST":
        doc_id = request.form.get("document_id")
        exam_date_str = request.form.get("exam_date")
        daily_hours = int(request.form.get("daily_hours", 2))

        cursor.execute("SELECT file_name, page_count FROM documents WHERE id = %s AND user_id = %s", (doc_id, user_id))
        doc = cursor.fetchone()

        if doc and exam_date_str:
            exam_date = dt.strptime(exam_date_str, "%Y-%m-%d").date()
            days_left = max((exam_date - date.today()).days, 1)
            total_pages = doc["page_count"] or 10

            plan_days = min(days_left, 7)
            pages_per_day = max(1, total_pages // plan_days)

            cursor.execute("DELETE FROM study_plans WHERE user_id = %s", (user_id,))
            db.commit()

            plan_rows = []
            display_plans = []
            for day_idx in range(1, plan_days + 1):
                start_pg = (day_idx - 1) * pages_per_day + 1
                end_pg = total_pages if day_idx == plan_days else day_idx * pages_per_day
                title = f"Module {day_idx}: Review Pages {start_pg} to {end_pg}"
                
                t_date = date.today() + datetime.timedelta(days=day_idx)
                start_time = "18:00:00"
                end_time = f"{min(23, 18 + daily_hours)}:00:00"

                plan_rows.append((user_id, title, t_date, start_time, end_time))
                display_plans.append({
                    "day": day_idx,
                    "date": t_date.strftime("%d %b %Y"),
                    "title": title,
                    "time": f"6:00 PM – {6 + daily_hours}:00 PM"
                })

            if plan_rows:
                cursor.executemany("""
                    INSERT INTO study_plans (user_id, plan_title, scheduled_date, start_time, end_time)
                    VALUES (%s, %s, %s, %s, %s)
                """, plan_rows)
                db.commit()

            cursor.close()
            db.close()

            return render_template(
                "plan_view.html",
                doc_name=doc["file_name"],
                plans=display_plans,
                days_left=days_left,
                daily_hours=daily_hours
            )

    cursor.execute("SELECT id, file_name FROM documents WHERE user_id = %s", (user_id,))
    documents = cursor.fetchall()
    cursor.close()
    db.close()
    return render_template("generate_plan.html", documents=documents)

# ---------------------------
# Settings & Preferences Route
# ---------------------------
@app.route("/settings", methods=["GET", "POST"])
def settings():
    if "user_id" not in session:
        return redirect("/")

    user_id = session["user_id"]
    db = get_db_connection()
    cursor = db.cursor(dictionary=True)

    message = None
    msg_type = "success"

    if request.method == "POST":
        action = request.form.get("action")
        
        if action == "update_profile":
            new_name = request.form.get("name", "").strip()
            if new_name:
                cursor.execute("UPDATE users SET name = %s WHERE id = %s", (new_name, user_id))
                db.commit()
                session["user_name"] = new_name
                message = "Profile name updated successfully!"
            else:
                message = "Name cannot be empty."
                msg_type = "error"

        elif action == "update_password":
            current_pwd = request.form.get("current_password", "")
            new_pwd = request.form.get("new_password", "")

            cursor.execute("SELECT password FROM users WHERE id = %s", (user_id,))
            user_data = cursor.fetchone()

            if user_data and check_password_hash(user_data["password"], current_pwd):
                if len(new_pwd) >= 6:
                    hashed = generate_password_hash(new_pwd)
                    cursor.execute("UPDATE users SET password = %s WHERE id = %s", (hashed, user_id))
                    db.commit()
                    message = "Password changed successfully!"
                else:
                    message = "New password must be at least 6 characters."
                    msg_type = "error"
            else:
                message = "Current password does not match."
                msg_type = "error"

        elif action == "update_preferences":
            session["engine_mode"] = request.form.get("engine_mode", "hybrid")
            session["default_study_hours"] = request.form.get("default_study_hours", "2")
            message = "Application preferences saved!"

    cursor.execute("SELECT name, email FROM users WHERE id = %s", (user_id,))
    user = cursor.fetchone()
    cursor.close()
    db.close()

    engine_mode = session.get("engine_mode", "hybrid")
    default_hours = session.get("default_study_hours", "2")

    return render_template(
        "settings.html",
        user=user,
        message=message,
        msg_type=msg_type,
        engine_mode=engine_mode,
        default_hours=default_hours
    )

if __name__ == "__main__":
    app.run(debug=True)