import streamlit as st
import requests
import tempfile
import os
import time
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

# --- Konfigurace ---
FAL_API_KEY = st.secrets["FAL_API_KEY"]
OPENCLAW_URL = st.secrets["OPENCLAW_URL"]
OPENCLAW_TOKEN = st.secrets["OPENCLAW_TOKEN"]
APP_PIN = st.secrets["PIN"]

PROJEKTY = {
    "Online psí škola": "Online psí škola",
    "Od snu k realitě": "Od snu k realite",
    "Stop úzkosti": "Stop úzkosti",
}

# --- Pomocné funkce ---

def load_system_prompt(projekt: str) -> str:
    """Načte system prompt a doplní název projektu."""
    prompt_path = os.path.join(os.path.dirname(__file__), "prompts", "ad_copy_system.txt")
    with open(prompt_path, "r", encoding="utf-8") as f:
        prompt = f.read()
    return prompt.replace("{projekt}", projekt)


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


def generate_ad_copy(transcriptions: dict[str, str], projekt: str) -> str:
    """Vygeneruje reklamní texty přes OpenClaw HTTP API."""
    system_prompt = load_system_prompt(projekt)

    user_content = "Zde jsou přepisy z reklamních reels videí:\n\n"
    for filename, text in transcriptions.items():
        user_content += f"### Video: {filename}\n{text}\n\n"
    user_content += "Na základě těchto přepisů vygeneruj 5 textů a 8 titulků podle instrukcí."

    resp = requests.post(
        f"{OPENCLAW_URL}/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {OPENCLAW_TOKEN}",
            "Content-Type": "application/json",
        },
        json={
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ]
        },
        timeout=120,
    )
    resp.raise_for_status()
    data = resp.json()
    return data["choices"][0]["message"]["content"]


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
            texty.append(cleaned)

    for line in titulek_section.strip().split("\n"):
        line = line.strip()
        match = re.match(r"\d+\.\s*(.*)", line)
        if match:
            titulky.append(match.group(1).strip())

    return texty, titulky


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

# Spuštění
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
            progress.progress(15)

        elif gdrive_url:
            folder_id = extract_gdrive_folder_id(gdrive_url)
            if not folder_id:
                st.error("Neplatný Google Drive odkaz. Zkontroluj URL.")
                st.stop()

            status.info("📥 Stahuji videa z Google Drive...")
            video_files = download_gdrive_folder(folder_id, tmp_dir)
            progress.progress(15)

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
            progress.progress(15 + int(25 * (i + 1) / len(video_files)))

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
                progress.progress(40 + int(35 * done_count / len(fal_urls)))
                status.info(f"🎙️ Přepsáno {done_count}/{len(fal_urls)} videí...")

        # Ukázat přepisy
        with st.expander("📝 Přepisy videí (klikni pro zobrazení)"):
            for fname, text in transcriptions.items():
                st.markdown(f"**{fname}:**")
                st.text(text)
                st.divider()

        # Krok 4: Generovat texty
        status.info("✍️ Generuji reklamní texty...")
        progress.progress(80)

        raw_result = generate_ad_copy(transcriptions, PROJEKTY[projekt])
        progress.progress(95)

        # Krok 5: Zobrazit výsledky
        status.success("✅ Hotovo!")
        progress.progress(100)

        texty, titulky = parse_results(raw_result)

        st.divider()
        st.header("📝 Reklamní texty")

        for i, text in enumerate(texty, 1):
            st.subheader(f"Text {i}")
            st.text_area(
                f"text_{i}",
                value=text,
                height=150,
                label_visibility="collapsed",
                key=f"text_area_{i}",
            )

        st.divider()
        st.header("🏷️ Titulky")

        for i, titulek in enumerate(titulky, 1):
            st.markdown(f"**{i}.** {titulek}  ({len(titulek)} znaků)")

        # Download tlačítko
        st.divider()
        full_output = "REKLAMNÍ TEXTY\n" + "=" * 40 + "\n\n"
        for i, text in enumerate(texty, 1):
            full_output += f"TEXT {i}\n{'-' * 20}\n{text}\n\n"
        full_output += "\nTITULKY\n" + "=" * 40 + "\n\n"
        for i, titulek in enumerate(titulky, 1):
            full_output += f"{i}. {titulek}\n"

        # Přidej i raw output pro případ špatného parsování
        full_output += "\n\n" + "=" * 40 + "\nKOMPLETNÍ RAW VÝSTUP:\n" + "=" * 40 + "\n\n"
        full_output += raw_result

        st.download_button(
            "📥 Stáhnout vše jako .txt",
            data=full_output,
            file_name="reklamni-texty.txt",
            mime="text/plain",
            use_container_width=True,
        )

    except Exception as e:
        st.error(f"Chyba: {e}")
        status.error(f"❌ Něco se pokazilo: {e}")

    finally:
        # Úklid tmp souborů
        import shutil
        if os.path.exists(tmp_dir):
            shutil.rmtree(tmp_dir, ignore_errors=True)
