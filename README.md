# Generátor reklamních textů z videí

Webová appka pro generování reklamních popisků a titulků z video reels.

## Jak to funguje

1. Vlož odkaz na Google Drive složku s videi (nebo nahraj MP4 přímo)
2. Vyber projekt (Psí škola / Od snu k realitě / Stop úzkosti)
3. Klikni "Generovat texty"
4. Počkej 2-5 minut
5. Zkopíruj nebo stáhni hotové texty

## Požadavky na Google Drive

- Složka musí být sdílená jako **"Kdokoli s odkazem"**
- Ve složce musí být MP4 soubory

## Technické info

- **Přepis videí:** fal.ai Whisper
- **Generování textů:** OpenClaw (claude-sonnet-4.6)
- **Hosting:** Streamlit Community Cloud
