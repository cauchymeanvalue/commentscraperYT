import hmac
import os

import streamlit as st
from dotenv import load_dotenv
from googleapiclient.discovery import build

import commentscraper_actual_job_script as core

load_dotenv()  # local: reads .env / Render: does nothing, env vars are already set

st.set_page_config(page_title="YouTube Comments", page_icon="💬")
st.title("💬 YouTube comment downloader")

# --- password gate: so strangers can't burn your API quota ---
password = os.environ.get("APP_PASSWORD", "")
entered = st.query_params.get("pw") or st.text_input("Password", type="password")
if not password or not hmac.compare_digest(entered, password):
    if entered:
        st.error("Wrong password.")
    st.stop()                                # nothing below this runs

# --- the actual app ---
link = st.text_input("YouTube video link")
fmt = st.radio("File type", ["txt", "csv"], horizontal=True)

if st.button("Get comments", type="primary"):
    video_id = core.extract_video_id(link)
    if not video_id:
        st.error("That doesn't look like a YouTube video link.")
        st.stop()
    counter = st.empty()                     # placeholder we can overwrite
    try:
        youtube = build("youtube", "v3", developerKey=os.environ["YT_API_KEY"],
                        cache_discovery=False)
        title = core.get_video_title(youtube, video_id)
        with st.spinner(f"Downloading comments for “{title}”…"):
            comments = core.get_all_comments(
                youtube, video_id, progress=lambda n: counter.caption(f"{n} so far…"))
        counter.empty()
        st.session_state["result"] = (title, video_id, comments)
    except core.FatalApiError as e:
        st.error(str(e))
        st.stop()

# --- show the download button if we have a result ---
if "result" in st.session_state:
    title, video_id, comments = st.session_state["result"]
    top = sum(not c["is_reply"] for c in comments)
    st.success(f"{top} comments and {len(comments) - top} replies from “{title}”")

    if fmt == "txt":
        data = core.txt_text(comments, title, video_id).encode("utf-8")
    else:
        data = core.csv_text(comments).encode("utf-8-sig")
    st.download_button("⬇️ Download file", data,
                       file_name=f"{core.safe_filename(title)}.{fmt}",
                       mime="text/csv" if fmt == "csv" else "text/plain")
