<img width="1280" height="400" alt="YouSly-Banner" src="https://github.com/user-attachments/assets/be0a8dc5-c616-40a1-8ed9-14c0954504d0" />

YouSly is a messaging tool that encodes/decodes messages into YouTube playlists using video metadata (title, creator, or video ID).
It supports strict positional matching, adaptive key shifts (hex only), and either API or no-API search modes.

# Why?
Dunno, felt like it

# Features
## Encode & decode me
ssages
- **Title**: Match characters against video titles
- **Creator**: Match characters against channel/creator names
- **Video ID**: Match characters against the 11-char YouTube video ID

## Strict matching only
- No relaxed fallback for final encoding
- Every encoded character must be an exact positional match
- If strict match fails, search expands and key-shift candidates are tried (hex positions only)

## Key behavior
- **`--key`**: Provide a hex key (`0-9a-f`)
- **`--otp`**: Generate a one-time-pad key (64 hex chars)
- Key loops across message length
- Effective key is printed after encode (use it for decode)

## Search/discovery modes
- **`--discover topic`**: Topic-driven search and expansion
- **`--discover trending`**: Trending-style discovery patterns
- **`--discover featured`**: Featured/recommended-style discovery patterns

## API and no-API modes
- **Search** can run via YouTube API or `yt-dlp` (`--no-api`)
- **Playlist writing** still uses OAuth YouTube API
- Decode supports API or `--no-api`

> [!WARNING]
> API tokens run out *very* quickly. Use the `--no-api` feature as much as possible for searching. When using this feature, it will only use the API to add videos to the playlist which is the most economical way to do it

## Reliability safeguards
- No video reuse across the same playlist encode run
- No per-character reuse of same video
- If playlist insertion fails (`videoNotFound`, etc.), YouSly auto-finds a replacement and retries

# Requirements
## Python libs
```bash
pip install google-api-python-client google-auth-oauthlib yt-dlp
```

## YouTube account setup
- User Settings (Top right corner) -> Create channel

## Google Cloud setup
- Enable **YouTube Data API v3**
- Create OAuth client and download `client_secret.json` into project root
- First write action stores auth token in `youtube_token.json`

### 1) Create API key (for search/read API mode)
- Google Cloud Console -> **APIs & Services** -> **Credentials**
- **Create credentials** -> **API key**
- (Optional but recommended) Restrict it to YouTube Data API v3 and your IPs

Set it in `yously.py`:
```python
YOUTUBE_API_KEY = "YOUR_KEY"
```

### 2) Create OAuth client (for playlist write + authenticated operations)
- Google Cloud Console -> **APIs & Services** -> **Credentials**
- **Create credentials** -> **OAuth client ID**
- App type: **Desktop app** (recommended)
- Add `http://localhost:43063/` to the Authorized redirect URIs
- Download the JSON and save as `client_secret.json` in this project directory

### 3) Configure OAuth consent screen
- **APIs & Services** -> **OAuth consent screen**
- If app is in testing, add your Google account under **Test users**

### 4) First auth run
- First encode run that writes a playlist (`--playlist-name ...`) will open auth flow
- Token is saved to `youtube_token.json`

### 5) yt-dlp runtime dependency (for `--no-api`)
Install at least one JS runtime for reliable extraction:
```bash
sudo apt update && sudo apt install -y nodejs
```
Then verify:
```bash
node --version || nodejs --version
```

# Usage
## Encode
### With manual key
```bash
python3 yously.py encode --message "secret text" --key "4f2a9721569ef978" --topic "machine learning" --discover topic --technique title --playlist-name "testing123" --no-api
```

### With OTP key
```bash
python3 yously.py encode --message "secret text" --otp --topic "winter olympics" --discover trending --technique creator --playlist-name "testing123" --no-api
```

<img width="1596" height="675" alt="encode-example" src="https://github.com/user-attachments/assets/83a026d3-0ec5-4b2a-8ce0-5a0a81015366" />
<img width="1306" height="816" alt="encode-example-youtube" src="https://github.com/user-attachments/assets/00759707-3668-4193-8a9e-a17a913e4f18" />

## Decode
### By playlist URL
```bash
python3 yously.py decode --playlist "https://www.youtube.com/playlist?list=PL..." --key "<effective_key_from_encode>" --technique creator
```

### By playlist ID
```bash
python3 yously.py decode --playlist "PL..." --key "<effective_key_from_encode>" --technique title
```

### Decode with no API
```bash
python3 yously.py decode --playlist "PL..." --key "<effective_key_from_encode>" --technique creator --no-api
```

<img width="806" height="94" alt="decode-example" src="https://github.com/user-attachments/assets/8508d7c9-326b-41f7-bba2-b0cb44e84734" />

# Options
## Encode
| Option | Description |
| --- | --- |
| `-m, --message` | Message to encode |
| `-k, --key` | Hex key (`0-9a-f`) |
| `--otp` | Generate 64-char hex OTP key |
| `--topic` | Topic hint for discovery |
| `--discover` | `topic`, `trending`, `featured` |
| `-t, --technique` | `title`, `creator`, `videoid` |
| `-p, --playlist-name` | Create/write playlist with this name |
| `--no-api` | Use `yt-dlp` for search only |
| `--results-per-search` | Approx result target per search (1-50, jittered) |
| `-v, -vv, -vvv` | Verbosity levels |

## Decode
| Option | Description |
| --- | --- |
| `-k, --key` | Hex key (use effective key from encode) |
| `-p, --playlist` | Playlist ID or full URL |
| `-t, --technique` | `title`, `creator`, `videoid` |
| `--no-api` | Read playlist metadata via `yt-dlp` |
| `-v, -vv, -vvv` | Verbosity levels |

# Technique notes
## `videoid`
- Message supports letters/digits/spaces only
- Space is matched using `_` substitute in IDs
- Key positions for matching are constrained to ID length semantics

## `creator`
Creator strings can differ between API and no-API sources.
If encode used `--no-api`, decode with `--no-api` is usually more consistent.

# How it works (high level)
## Encoding
1. Build discovery query set from topic/mode
2. Search YouTube and collect candidates
3. Strict positional check per character
4. Expand search (next pages, related/recommended, dynamic topic variants)
5. Apply key-shift if needed (hex positions only)
6. Write playlist and auto-replace unavailable insertions
7. Print final summary + decode command

## Decoding
1. Read playlist items (API or no-API)
2. Select source field by technique (title/creator/videoid)
3. Apply key positions in order (looping as needed)
4. Emit decoded message

# OPSEC
I did this just to muck around and see whether it could be done.

| **Feature** | **Level 0** | **Level 1** | **Level 2** |
| --- | :---: | :---: | :---: |
| Topic/discovery noise | ❌ | ✅ | ✅ |
| Random jitter delays | ❌ | ✅ | ✅ |
| Related video exploration | ❌ | ✅ | ✅ |
| Fuzzy queries & typos | ❌ | ❌ | ✅ |
| Human-like extra browsing | ❌ | ❌ | ✅ |
| Add/remove mistake simulation | ❌ | ❌ | ✅ |

> [!IMPORTANT]
> Even with OPSEC enabled, this still generates automation-like request patterns (YouTube Data API and/or yt-dlp extractor traffic), not a real browser session from youtube.com UI interactions. If someone is actually analyzing traffic deeply, it can still look automated.

`sleep_with_jitter()`
- Adds randomized delays to simulate natural pauses/distraction

`simulate_noise()`
- **Level 1**:
  - Runs noise searches around the topic
  - Performs lightweight preview-like related-video lookups
- **Level 2**:
  - Adds fuzzy/typo-style queries
  - Increases search and preview depth

`simulate_human_browsing()`
- **Level 2 only**
- Runs additional browsing-style searches (playlist/docu/reaction/live-style patterns)

`maybe_add_remove_mistake()`
- **Level 2 only**
- Adds a non-encoded “wrong” video to the playlist, waits, then removes it to mimic accidental user behavior

# Important notes
- Use the **Effective Key** from encode when decoding
- If key shift is enabled, original key may not decode correctly
- Search can still take time on strict hard characters
- YouTube metadata can change; decode reliability is best when done soon after encode

# Test commands
Below is a practical command set to test major variants of the tool.

## Encode tests (unique playlist names)
### 1) `title` + `topic` + `--no-api` + OTP + OPSEC 0
```bash
python3 yously.py encode --message "secret text" --otp --topic "machine learning" --discover topic --technique title --playlist-name "yously-test-title-topic-noapi-01" --no-api --opsec 0
```

### 2) `creator` + `trending` + `--no-api` + provided key + OPSEC 1
```bash
python3 yously.py encode --message "secret text" --key "4f2a9721569ef978" --topic "winter olympics" --discover trending --technique creator --playlist-name "yously-test-creator-trending-noapi-02" --no-api --opsec 1
```

### 3) `videoid` + `featured` + `--no-api` + OTP + OPSEC 2
```bash
python3 yously.py encode --message "secret text" --otp --topic "donald trump" --discover featured --technique videoid --playlist-name "yously-test-videoid-featured-noapi-03" --no-api --opsec 2
```

### 4) API search mode (`--no-api` omitted) + `title` + provided key
```bash
python3 yously.py encode --message "secret text" --key "4f2a9721569ef978" --topic "space exploration" --discover topic --technique title --playlist-name "yously-test-title-api-04" --opsec 1
```

### 5) API search mode + `creator` + OTP + featured
```bash
python3 yously.py encode --message "secret text" --otp --topic "counter strike" --discover featured --technique creator --playlist-name "yously-test-creator-api-05" --opsec 2
```

### 6) `videoid` + topic discovery + provided key
```bash
python3 yously.py encode --message "secret text" --key "4f2a9721569ef978" --topic "formula 1" --discover topic --technique videoid --playlist-name "yously-test-videoid-topic-06" --no-api --opsec 0
```

## Decode tests
For each encode run, use the **Effective Key** printed in its summary.
You can pass either full playlist URL or playlist ID.

### Decode by URL (API mode)
```bash
python3 yously.py decode --playlist "https://www.youtube.com/playlist?list=PLAYLIST_ID_HERE" --key "EFFECTIVE_KEY_HERE" --technique title
```

### Decode by URL (`--no-api`)
```bash
python3 yously.py decode --playlist "https://www.youtube.com/playlist?list=PLAYLIST_ID_HERE" --key "EFFECTIVE_KEY_HERE" --technique creator --no-api
```

### Decode by playlist ID (API mode)
```bash
python3 yously.py decode --playlist "PLAYLIST_ID_HERE" --key "EFFECTIVE_KEY_HERE" --technique videoid
```

### Decode by playlist ID (`--no-api`)
```bash
python3 yously.py decode --playlist "PLAYLIST_ID_HERE" --key "EFFECTIVE_KEY_HERE" --technique title --no-api
```
