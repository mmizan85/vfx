
# ⚡ vfx (Video Flow X-Downloader)

<p align="center">
  <img width="120" alt="vfx_icon01" src="https://github.com/user-attachments/assets/b9f831a9-0441-4fb1-8bc0-65365e7bcfd5" />

</p>

<p align="center">
  <strong>The Ultimate Production-Grade Media Downloader Engine for Spotify, YouTube, FB & More!</strong>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.11%2B-blue?style=for-the-badge&logo=python" alt="Python Version">
  <img src="https://img.shields.io/badge/Platform-Windows-green?style=for-the-badge&logo=windows" alt="Platform">
  <img src="https://img.shields.io/badge/License-MIT-yellow?style=for-the-badge" alt="License">
</p>

---

## 🌟 Overview

**`vfx`** (Video Flow X-Downloader) is a highly advanced, production-grade command-line interface (CLI) media utility designed to download videos, audio, tracks, and complete playlists from major streaming platforms. It bridges the gap between Spotify and YouTube by extracting metadata (such as Title, Artist, and Cover Art) from Spotify links, searching for the highest available quality stream on YouTube/YT Music, and processing it locally with full ID3 tagging and thumbnail embedding.

---

## 🚀 Advanced Features

* 📱 **Responsive UI:** The interface dynamically adapts and wraps text, tables, and progress bars beautifully according to the terminal window size.
* 🔗 **Smart Spotify Integration:** Seamlessly handles Spotify tracks, albums, or playlist URLs using the official Spotify Web API.
* ⚡ **Multi-Threaded Performance:** Powered by a background `ThreadPoolExecutor` to handle concurrent downloads for large playlists without slowing down or freezing the operating system.
* 📞 **Feature Phone Optimization:** Contains specialized quick presets to downscale and re-encode videos to low-resolutions (e.g., 320x240 or 176x144 in .mp4/.3gp format) designed specifically for button/feature mobiles.
* 📂 **Automated Playlist Isolation:** When a playlist or album is detected, `vfx` automatically extracts its title, generates a designated sub-folder, and organizes all processed files neatly inside it.
* ⚙️ **All-in-One Control Panel:** Accessible via the `--config` parameter, allowing complete, advance control over API credentials, default download paths, maximum thread counts, metadata injections, and download speed limits.
* 🔄 **Smart Resume & Duplicate Prevention:** Utilizes atomic file/local database checking to skip already downloaded tracks, minimizing bandwidth usage and recovering gracefully from sudden network drops.

---

## 🛠️ System Architecture & Backend Core

The core framework of `vfx` is structured to ensure maximum crash resistance and high-throughput execution:


```text
[User Input URL] ➔ [vfx Core Engine] ➔ Regex Validation
│
┌─────────────────────┴─────────────────────┐
▼                                           ▼
[Spotify Link Detected]                    [Direct Video Link]
│                                           │
[Spotipy API Module]                       [yt-dlp core engine]
│                                           │
Extract: Title, Artist, Tags                │
│                                           │
(Auto Search)                               ▼
[yt-dlp Core] ◄─────────────────────────── Download Source File
│
▼
[FFmpeg Post-Processor] ➔ Embed High-Res Thumbnail & Write ID3 Metadata Tags
│
▼
[Final File Output] ➔ Auto Folder Isolation & Smart History Check
```

### 🧰 Powered By:
1. **Python 3.11+** - Pure Object-Oriented Programming (OOP) modular architecture.
2. **yt-dlp Module** - Core streaming and network extraction engine.
3. **FFmpeg Engine** - Post-processing, high-fidelity audio extraction, re-encoding, and atomic metadata writing.
4. **Spotipy (Spotify Web API)** - Remote metadata lookup and track itemization.
5. **Rich & Questionary** - Beautiful animated progress bars, styling, layout responsive elements, and interactive directional prompt selection menus.

---

## 📸 Screenshots / Preview

*(Place your application terminal recordings or screenshots inside an `assets/` folder in your repository to display them here)*

| 📱 Main Menu & Layout | 📊 Live Progress Bar |
| :---: | :---: |
| <img width="500"  alt="main_menu" src="https://github.com/user-attachments/assets/3b950705-a088-4edb-90ac-08fafa6c3a30" /> | <img width="500" alt="progress_bar" src="https://github.com/user-attachments/assets/e50ac83a-204d-4f63-a452-be85fe072e7d" /> |

| ⚙️ Control Panel (`--config`) | 📁 Download Summary Table |
| :---: | :---: |
| <img width="500"  alt="config" src="https://github.com/user-attachments/assets/fadc117f-9ed2-449e-94e2-c51697c36ce8" /> |  <img width="500" alt="summar" src="https://github.com/user-attachments/assets/95c3b61e-9f53-4f61-ab77-108be6ad3210" /> |

---

## 💻 Installation & Usage Guide

You can install and use `vfx` using either of the two methods below:

### Method 1: Windows Installer (Easiest Method 🚀)

Perfect for general users. No need to install Python, FFmpeg, or manage paths manually:

1. Go to the [**Releases**](https://github.com/mmizan85/vfx/releases/download/v1.0.0/vfx_setup_v1.0.0.exe) section on the right side of this GitHub page and download `vfx_setup_v1.0.0.exe`.
2. Run the installer wizard and follow the on-screen instructions.
3. **Crucial:** Make sure to check the box that says **"Add VFX to System PATH"** during installation.
4. Open a fresh terminal window (Command Prompt or PowerShell) and run: `vfx --help`


---

### Method 2: Running from Source Code (For Developers 🧑‍💻)

**Step 1: Clone the repository and navigate into it**

```bash
git clone https://github.com/mmizan85/vfx.git

cd vfx
```

**Step 2: Install dependencies**

```bash
pip install -r requirements.txt
```

*(Note: Ensure `ffmpeg.exe` and `yt-dlp.exe` are placed in your system path or inside the root folder of the project.)*

**Step 3: Launch the application**

```bash
python main.py
```

---

## 🕹️ Command Parameters & Usage Rules

While `vfx` offers an entirely interactive step-by-step UI menu, you can bypass it and call actions directly using native CLI arguments:

```bash
vfx [OPTIONS] <URL>
```

### 📋 Supported Parameter Flags:

```text
usage: vfx [-h] [--config] [--version] [url]

🚀 VFX — download from Spotify, YouTube, and more.

positional arguments:
  url         Media URL (Spotify, YouTube, or any yt-dlp-supported site). Omit to be prompted interactively.

options:
  -h, --help  show this help message and exit
  --config    Open the interactive configuration panel.
  --version   show program's version number and exit
```

### 💡 Syntax Examples:

* **Download a Spotify Playlist as High-Fidelity 320kbps MP3s:**
```bash
vfx  https://open.spotify.com/playlist/your_playlist_id
```


* **Download a YouTube Video Optimized specifically for Feature Phones:**
```bash
vfx https://youtu.be/example_video_id
```


* **⚙️ VFX Configuration:**
```bash
vfx  --config
```
* **VFX-Configure**

#### ? What would you like to configure? (Use arrow keys)
 - » 🟢  Spotify API Credentials
 -   📁  Default Download Path
 -   ⚙️  Concurrency (thread count)
 -   🚀  Default Preset Selection
 -   ⏱️  Rate Limit (speed cap)
 -   ✨  Feature Toggles (metadata, lyrics, auto-update...)
 -   🔗  Cookies / Age-Restricted Content
 -   🛠️  FFmpeg path override
 -   ✅  Save & Exit
 -   ❌  Discard & Exit


---

## 🤝 Contributing & Updates

We aim to make `vfx` the most bulletproof and feature-complete open-source downloader utility available. If you are a Python Developer or UI/UX enthusiast, we highly welcome your updates, pull requests, and bug fixes:

1. **Fork** the repository.
2. Create a dedicated feature branch (`git checkout -b feature/AmazingFeature`).
3. Commit your localized adjustments safely (`git commit -m 'Add some AmazingFeature'`).
4. Push your modifications up to your branch (`git push origin feature/AmazingFeature`).
5. Open a **Pull Request** right here for code review!

### 📌 Current Roadmap:

* [ ] Automated Synced Lyrics fetching and inner subtitle mapping (`.lrc`).
* [ ] Direct cloud backup uploading integrations (Google Drive/Dropbox API).

---

## 👨‍💻 Developer & Credits

* **Lead Architect:** Mohammad Mizanur Rahman (Mizan)
* **Special Acknowledgments:** A massive thank you to the open-source engineering communities driving `yt-dlp` and `FFmpeg`.

---

Developed with ❤️ in 2026.

