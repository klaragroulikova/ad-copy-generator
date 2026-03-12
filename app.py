import streamlit as st
import requests
import tempfile
import os
import time
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

# --- Konfigurace ---
FAL_API_KEY = st.secrets["FAL_API_KEY"]
GEMINI_API_KEY = st.secrets["GEMINI_API_KEY"]
APP_PIN = st.secrets["PIN"]

GEMINI_MODEL = "gemini-2.0-flash"
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"

PROJEKTY = {
    "Online psí škola": "Online psí škola",
    "Od snu k realitě": "Od snu k realite",
    "Stop úzkosti": "Stop úzkosti",
}

# --- Pomocné funkce ---


def load_prompt(filename: str, **kwargs) -> str:
    """Načte prompt soubor a doplní proměnné."""
    prompt_path = os.path.join(os.path.dirname(__file__), "prompts", filename)
    with open(prompt_path, "r", encoding="utf-8") as f:
        prompt = f.read()
    for key, value in kwargs.items():
        prompt = prompt.replace(f"{{{key}}}", str(value))
    return prompt


def extract_gdrive_folder_id(url: str) -> str | None:
    """Vytáhne folder ID z Google Drive URL."""
    match = re.search(r"folders/([a-zA-Z0-9_-]+)", url)
    return match.group(1) if match else None


def download_gdrive_folder(folder_id: str, output_dir: str, status_callback=None) -> list[str]:
    """Stáhne MP4 soubory z Google Drive složky pomocí gdown."""
    import gdown

    if status_callback:
        status_callback("Stahuji seznam souborů z Google Drive...")

    files = gdown.download_folder(
        id=folder_id,
        output=output_dir,
        quiet=True,
        remaining_ok=True,
    )

    mp4_files = []
    if files:
        for f in files:
            if isinstance(f, str) and f.endswith(".mp4"):
                mp4_files.append(f)

    if not mp4_files:
        for f in os.listdir(output_dir):
            if f.endswith(".mp4"):
                mp4_files.append(os.path.join(output_dir, f))

    return sorted(mp4_files)


def upload_to_fal(filepath: str) -> str:
    """Nahraje soubor na fal.ai storage a vrátí CDN URL."""
    filename = os.path.basename(filepath)

    init_resp = requests.post(
        "https://rest.alpha.fal.ai/storage/upload/initiate",
        headers={
            "Authorization": f"Key {FAL_API_KEY}",
            "Content-Type": "application/json",
        },
        json={"file_name": filename, "content_type": "video/mp4"},
    )
    init_resp.raise_for_status()
    data = init_resp.json()

    with open(filepath, "rb") as f:
        upload_resp = requests.put(
            data["upload_url"],
            headers={"Content-Type": "video/mp4"},
            data=f,
        )
        upload_resp.raise_for_status()

    return data["file_url"]


def transcribe_video(fal_url: str) -> str:
    """Přepíše video pomocí fal.ai Whisper. Vrátí český text."""
    submit_resp = requests.post(
        "https://queue.fal.run/fal-ai/whisper",
        headers={
            "Authorization": f"Key {FAL_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "audio_url": fal_url,
            "task": "transcribe",
            "language": "cs",
        },
    )
    submit_resp.raise_for_status()
    job = submit_resp.json()

    status_url = job["status_url"]
    response_url = job["response_url"]

    for _ in range(120):
        time.sleep(2)
        status_resp = requests.get(
            status_url,
            headers={"Authorization": f"Key {FAL_API_KEY}"},
        )
        status_data = status_resp.json()
        if status_data.get("status") == "COMPLETED":
            break
        if status_data.get("status") == "FAILED":
            return "[Chyba: Přepis se nezdařil]"
    else:
        return "[Chyba: Timeout při přepisu]"

    result_resp = requests.get(
        response_url,
        headers={"Authorization": f"Key {FAL_API_KEY}"},
    )
    result_data = result_resp.json()

    if "text" in result_data:
        return result_data["text"].strip()
    return "[Chyba: Prázdný přepis]"


def call_gemini(system_prompt: str, user_content: str) -> str:
    """Zavolá Google Gemini API (ZDARMA) a vrátí odpověď."""
    resp = requests.post(
        f"{GEMINI_URL}?key={GEMINI_API_KEY}",
        headers={"Content-Type": "application/json"},
        json={
            "system_instruction": {
                "parts": [{"text": system_prompt}]
            },
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": user_content}]
                }
            ],
            "generationConfig": {
                "temperature": 0.7,
                "maxOutputTokens": 4096,
            }
        },
        timeout=180,
    )
    resp.raise_for_status()
    data = resp.json()
    return data["candidates"][0]["content"]["parts"][0]["text"]


def generate_ad_copy(transcriptions: dict[str, str], projekt: str) -> str:
    """Vygeneruje reklamní texty přes Gemini — 1 text na video + 8 titulků."""
    pocet = len(transcriptions)
    system_prompt = load_prompt(
        "ad_copy_system.txt",
        projekt=projekt,
        pocet_videi=pocet,
    )

    user_content = "Zde jsou přepisy z reklamních reels videí:\n\n"
    for i, (filename, text) in enumerate(transcriptions.items(), 1):
        user_content += f"### Video {i}: {filename}\n{text}\n\n"
    user_content += f"Na základě těchto přepisů vygeneruj {pocet} textů (jeden ke každému videu) a 8 titulků podle instrukcí."

    return call_gemini(system_prompt, user_content)


def correct_czech(raw_text: str) -> str:
    """Opraví češtinu ve vygenerovaných textech (2. průchod)."""
    system_prompt = load_prompt("czech_correction.txt")
    return call_gemini(system_prompt, raw_text)


def edit_texts(current_texts: str, instruction: str, transcriptions_text: str) -> str:
    """Upraví texty podle uživatelské instrukce."""
    system_prompt = load_prompt("edit_instruction.txt")

    user_content = f"## AKTUÁLNÍ TEXTY:\n\n{current_texts}\n\n"
    if transcriptions_text:
        user_content += f"## PŘEPISY VIDEÍ (pro kontext):\n\n{transcriptions_text}\n\n"
    user_content += f"## INSTRUKCE K ÚPRAVĚ:\n\n{instruction}"

    return call_gemini(system_prompt, user_content)


def parse_results(raw_text: str) -> tuple[list[str], list[str]]:
    """Rozparsuje výstup na texty a titulky."""
    texty = []
    titulky = []

    text_section = raw_text.split("---TITULKY---")[0] if "---TITULKY---" in raw_text else raw_text
    titulek_section = raw_text.split("---TITULKY---")[1] if "---TITULKY---" in raw_text else ""

    text_blocks = re.split(r"\*\*TEXT \d+\*\*[^\n]*\n", text_section)
    for block in text_blocks:
        cleaned = block.strip()
        if cleaned and "---TEXTY---" not in cleaned:
            # Odstraň oddělovač ---
            cleaned = re.sub(r"\n?---\s*$", "", cleaned).strip()
            texty.append(cleaned)

    for line in titulek_section.strip().split("\n"):
        line = line.strip()
        match = re.match(r"\d+\.\s*(.*)", line)
        if match:
            titulky.append(match.group(1).strip())

    return texty, titulky


def format_transcriptions_text(transcriptions: dict[str, str]) -> str:
    """Formátuje přepisy do čitelného textu."""
    text = ""
    for fname, transcript in transcriptions.items():
        text += f"### {fname}\n{transcript}\n\n"
    return text.strip()


def build_raw_output(texty: list[str], titulky: list[str]) -> str:
    """Sestaví raw text ve formátu ---TEXTY--- / ---TITULKY---."""
    styles = [
        "(krátký, bez emoji)",
        "(delší, s emoji, příběhový)",
        "(střední, bez emoji, direct response)",
        "(krátký, s emoji, sociální důkaz)",
        "(delší, edukační, mix emoji)",
    ]
    output = "---TEXTY---\n\n"
    for i, text in enumerate(texty):
        style = styles[i % len(styles)] if styles else ""
        output += f"**TEXT {i+1}** {style}\n{text}\n\n"
    output += "---TITULKY---\n\n"
    for i, titulek in enumerate(titulky, 1):
        output += f"{i}. {titulek}\n"
    return output


def build_download_output(
    texty: list[str],
    titulky: list[str],
    transcriptions: dict[str, str] | None = None,
) -> str:
    """Sestaví kompletní výstup ke stažení."""
    full_output = "REKLAMNÍ TEXTY\n" + "=" * 40 + "\n\n"
    for i, text in enumerate(texty, 1):
        full_output += f"TEXT {i}\n{'-' * 20}\n{text}\n\n---\n\n"
    full_output += "\nTITULKY\n" + "=" * 40 + "\n\n"
    for i, titulek in enumerate(titulky, 1):
        full_output += f"{i}. {titulek}\n"

    if transcriptions:
        full_output += "\n\n" + "=" * 40 + "\nPŘEPISY VIDEÍ:\n" + "=" * 40 + "\n\n"
        for fname, text in transcriptions.items():
            full_output += f"### {fname}\n{text}\n\n"

    return full_output


# --- Streamlit UI ---

st.set_page_config(
    page_title="Generátor reklamních textů",
    page_icon="🎬",
    layout="centered",
)

st.title("🎬 Generátor reklamních textů z videí")
st.caption("Vlož odkaz na Google Drive složku s reels a dostaneš hotové popisky + titulky.")

# PIN ochrana
if "authenticated" not in st.session_state:
    st.session_state.authenticated = False

if not st.session_state.authenticated:
    pin = st.text_input("Zadej PIN:", type="password", max_chars=10)
    if st.button("Přihlásit"):
        if pin == APP_PIN:
            st.session_state.authenticated = True
            st.rerun()
        else:
            st.error("Špatný PIN.")
    st.stop()

# Inicializace session state
if "texty" not in st.session_state:
    st.session_state.texty = []
if "titulky" not in st.session_state:
    st.session_state.titulky = []
if "transcriptions" not in st.session_state:
    st.session_state.transcriptions = {}
if "raw_result" not in st.session_state:
    st.session_state.raw_result = ""
if "edit_history" not in st.session_state:
    st.session_state.edit_history = []
if "generated" not in st.session_state:
    st.session_state.generated = False

# Hlavní formulář
st.divider()

col1, col2 = st.columns([3, 1])
with col1:
    gdrive_url = st.text_input(
        "Google Drive odkaz na složku s videi:",
        placeholder="https://drive.google.com/drive/folders/...",
    )
with col2:
    projekt = st.selectbox("Projekt:", list(PROJEKTY.keys()))

# Fallback: přímý upload
with st.expander("Nebo nahraj videa přímo"):
    uploaded_files = st.file_uploader(
        "Vyber MP4 soubory:",
        type=["mp4"],
        accept_multiple_files=True,
    )

# Spuštění generování
if st.button("🚀 Generovat texty", type="primary", use_container_width=True):
    progress = st.progress(0)
    status = st.empty()

    video_files = []
    tmp_dir = tempfile.mkdtemp()

    try:
        # Krok 1: Získat videa
        if uploaded_files:
            status.info("📁 Ukládám nahraná videa...")
            for uf in uploaded_files:
                path = os.path.join(tmp_dir, uf.name)
                with open(path, "wb") as f:
                    f.write(uf.getbuffer())
                video_files.append(path)
            progress.progress(10)

        elif gdrive_url:
            folder_id = extract_gdrive_folder_id(gdrive_url)
            if not folder_id:
                st.error("Neplatný Google Drive odkaz. Zkontroluj URL.")
                st.stop()

            status.info("📥 Stahuji videa z Google Drive...")
            video_files = download_gdrive_folder(folder_id, tmp_dir)
            progress.progress(10)

            if not video_files:
                st.error("Ve složce nejsou žádné MP4 soubory. Zkontroluj odkaz a sdílení ('Kdokoli s odkazem').")
                st.stop()
        else:
            st.warning("Zadej Google Drive odkaz nebo nahraj videa.")
            st.stop()

        st.info(f"Nalezeno **{len(video_files)} videí**: {', '.join(os.path.basename(f) for f in video_files)}")

        # Krok 2: Upload na fal.ai
        status.info("☁️ Nahrávám videa na server pro přepis...")
        fal_urls = {}
        for i, vf in enumerate(video_files):
            fname = os.path.basename(vf)
            status.info(f"☁️ Nahrávám {fname} ({i+1}/{len(video_files)})...")
            fal_url = upload_to_fal(vf)
            fal_urls[fname] = fal_url
            progress.progress(10 + int(20 * (i + 1) / len(video_files)))

        # Krok 3: Whisper přepis (paralelně)
        status.info("🎙️ Přepisuji videa do textu (může trvat 1-2 minuty)...")
        transcriptions = {}

        def do_transcribe(item):
            fname, url = item
            return fname, transcribe_video(url)

        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = {executor.submit(do_transcribe, item): item for item in fal_urls.items()}
            done_count = 0
            for future in as_completed(futures):
                fname, text = future.result()
                transcriptions[fname] = text
                done_count += 1
                progress.progress(30 + int(25 * done_count / len(fal_urls)))
                status.info(f"🎙️ Přepsáno {done_count}/{len(fal_urls)} videí...")

        # Uložit přepisy do session state
        st.session_state.transcriptions = transcriptions

        # Krok 4: Generovat texty (1 text na video + 8 titulků)
        status.info(f"✍️ Generuji {len(transcriptions)} textů + 8 titulků...")
        progress.progress(60)

        raw_result = generate_ad_copy(transcriptions, PROJEKTY[projekt])
        progress.progress(75)

        # Krok 5: Korekce češtiny (2. průchod)
        status.info("🔍 Koriguju češtinu...")
        corrected_result = correct_czech(raw_result)
        progress.progress(90)

        # Krok 6: Uložit výsledky
        st.session_state.raw_result = corrected_result
        texty, titulky = parse_results(corrected_result)
        st.session_state.texty = texty
        st.session_state.titulky = titulky
        st.session_state.generated = True
        st.session_state.edit_history = []

        status.success(f"✅ Hotovo! {len(texty)} textů + {len(titulky)} titulků vygenerováno a zkorigováno.")
        progress.progress(100)

    except Exception as e:
        st.error(f"Chyba: {e}")
        status.error(f"❌ Něco se pokazilo: {e}")

    finally:
        import shutil
        if os.path.exists(tmp_dir):
            shutil.rmtree(tmp_dir, ignore_errors=True)


# --- Zobrazení výsledků (z session state) ---

if st.session_state.generated and st.session_state.texty:
    texty = st.session_state.texty
    titulky = st.session_state.titulky
    transcriptions = st.session_state.transcriptions

    # Přepisy videí
    if transcriptions:
        st.divider()
        with st.expander("🎙️ Přepisy videí (klikni pro zobrazení)"):
            for fname, text in transcriptions.items():
                st.markdown(f"**{fname}:**")
                st.text(text)
                st.divider()

    # Reklamní texty
    st.divider()
    st.header(f"📝 Reklamní texty ({len(texty)})")

    for i, text in enumerate(texty, 1):
        st.subheader(f"Text {i}")
        st.text_area(
            f"text_{i}",
            value=text,
            height=150,
            label_visibility="collapsed",
            key=f"text_area_{i}_{len(st.session_state.edit_history)}",
        )

    # Titulky
    st.divider()
    st.header("🏷️ Titulky")

    for i, titulek in enumerate(titulky, 1):
        st.markdown(f"**{i}.** {titulek}  ({len(titulek)} znaků)")

    # Download tlačítko
    st.divider()
    full_output = build_download_output(texty, titulky, transcriptions)

    st.download_button(
        "📥 Stáhnout vše jako .txt (včetně přepisů)",
        data=full_output,
        file_name="reklamni-texty.txt",
        mime="text/plain",
        use_container_width=True,
    )

    # --- Chat pro úpravy ---
    st.divider()
    st.header("✏️ Úpravy textů")
    st.caption(
        "Napiš instrukci a texty se automaticky upraví. "
        "Např. *„přepiš text 4"*, *„přidej víc emoji"*, *„zkrať text 2"*"
    )

    # Historie úprav
    for edit in st.session_state.edit_history:
        st.chat_message("user").write(edit["instruction"])
        st.chat_message("assistant").write("✅ Texty upraveny.")

    # Input pro novou úpravu
    edit_instruction = st.chat_input("Napiš co chceš upravit...")

    if edit_instruction:
        with st.spinner("✍️ Upravuji texty..."):
            try:
                # Sestav aktuální texty jako raw
                current_raw = build_raw_output(texty, titulky)

                # Přepisy pro kontext
                trans_text = format_transcriptions_text(transcriptions) if transcriptions else ""

                # Zavolej Gemini s instrukcí
                edited_result = edit_texts(current_raw, edit_instruction, trans_text)

                # Parsuj nové texty
                new_texty, new_titulky = parse_results(edited_result)

                if new_texty:
                    st.session_state.texty = new_texty
                if new_titulky:
                    st.session_state.titulky = new_titulky
                st.session_state.raw_result = edited_result
                st.session_state.edit_history.append({
                    "instruction": edit_instruction,
                })

                st.rerun()

            except Exception as e:
                st.error(f"Chyba při úpravě: {e}")
