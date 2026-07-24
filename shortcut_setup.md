# Apple Shortcuts Setup

Two iOS Shortcuts connect the Share Sheet to your Margin server — one for
URLs, one for PDFs. The server can run anywhere reachable from the phone:
a Mac on your network, an Ubuntu server, or any machine over Tailscale.

> **Why two shortcuts instead of one?**
> Shortcuts cannot reliably branch on whether the input is a URL vs a file — the
> `If … is URL` condition is inconsistent across share sources (Safari, RSS readers,
> Files app). The solution is two focused shortcuts, each accepting only the type it
> handles. iOS shows each shortcut only when the Share Sheet input matches its
> declared type, so the right one always appears.

| Name | Accepts | Talks to |
|---|---|---|
| **Save to Margin** | URLs | your Margin server (recommended) |
| **Save PDF to Margin** | PDF files | your Margin server (recommended) |
| **Save to Margin (no server)** | URLs | Mathpix API directly — see appendix |
| **Save PDF to Margin (no server)** | PDF files | Mathpix API directly — see appendix |

The server shortcuts are thin clients: they POST the URL or file to Margin and
show a notification with the result. All conversion logic stays server-side.
The "no server" variants in the appendix call the Mathpix API straight from
iOS and are only worth building if you can't run the server at all.

---

## Prerequisites

- The Margin server is running and reachable:
  - **Ubuntu**: `systemctl status margin` on the server
  - **macOS**: started via `bash start.sh` or its Launch Agent
  - From any machine: `curl http://SERVER_ADDRESS:8000/health` returns JSON
- Find the server address to put into the shortcuts:
  - Same Wi-Fi / LAN → the server machine's local IP, e.g.
    `http://192.168.1.42:8000`
  - Away from home → install [Tailscale](https://tailscale.com) on the phone
    and the server; use the server's Tailscale IP, e.g. `http://100.x.y.z:8000`
    (full guide: "Remote access via Tailscale" in the README)
- If the server sets `MARGIN_TOKEN`, each shortcut needs one extra header —
  the build steps below mention where.

---

## Shortcut 1 of 2 — "Save to Margin"

**Step 1 — Create and configure**

1. Open the **Shortcuts** app → tap **+** (top right).
2. Tap the title field at the top → type **Save to Margin** → tap **Done**.
3. Tap **ⓘ** (bottom right) → turn on **Show in Share Sheet**.
4. Tap **Share Sheet Types** → deselect everything except **URLs** → tap **Done**.
5. Tap **Done** to close the details panel.

**Step 2 — Add the request action**

6. Tap **Add Action** → search for **Get Contents of URL** → tap it.
7. Tap the blue **URL** field in the action → type your server address:
   `http://SERVER_ADDRESS:8000/save`
   (This is the unified endpoint — it produces the server's default formats,
   `pdf,md,tex` out of the box, the same as the desktop bookmarklet. To pin
   this shortcut to specific formats regardless of the server default, add a
   second Body field below — Type **Text**, Key `formats`, Value e.g.
   `pdf,md`. Older setups posted to `/save-url`, which only ever wrote
   Markdown; switching the URL to `/save` is all that's needed to match.)
8. Tap **Show More** (below the URL field) to expand the options.
9. Set **Method** to **POST**.
10. Set **Request Body** to **JSON**.
11. Tap **Add new field** → set Type to **Text**.
    - Key: `url`
    - Value: tap the value field → tap the variable icon (the `x` in a circle) →
      select **Shortcut Input**
12. In the **Headers** section (separate from Body in iOS 16+), tap **Add new
    field**. Set Key: `Accept`, Value: `application/json`. This tells the
    server the client expects JSON back, so the response is parsed correctly
    by the next steps.
    *If your server sets `MARGIN_TOKEN`*: add a second header — Key:
    `Authorization`, Value: `Bearer YOUR-TOKEN` (the word "Bearer", a space,
    then the token).

**Step 3 — Parse the response**

13. Tap **Add Action** → search for **Get Dictionary from Input** → tap it.
    - Input: tap the field → select **Contents of URL**
    - This explicitly parses the JSON response into a Dictionary. Without this
      step, Shortcuts sometimes treats a JSON response as plain Text and the next
      step fails with "couldn't convert from Text to Dictionary".

14. Tap **Add Action** → **Get Dictionary Value** → Key: `summary`,
    Dictionary: **Dictionary**. The server pre-formats this one field for
    notifications — `Saved: <filename>` on success, `Error: <reason>` on
    failure, `Already saved: <filename>` for duplicates — so no further
    dictionary lookups or text assembly are needed.

**Step 4 — Notify**

15. Tap **Add Action** → search for **Show Notification** → tap it.
    - Title: `Margin`
    - Body: tap the field → tap the variable icon (the `x` in a circle) →
      pick **Dictionary Value**.

16. Tap **Done** (top right).

---

## Shortcut 2 of 2 — "Save PDF to Margin"

**Step 1 — Create and configure**

1. Open **Shortcuts** → tap **+**.
2. Name it **Save PDF to Margin**.
3. Tap **ⓘ** → turn on **Show in Share Sheet**.
4. Tap **Share Sheet Types** → deselect everything except **PDFs** → tap **Done**.
5. Tap **Done** to close the details panel.

**Step 2 — Add the request action**

6. Tap **Add Action** → **Get Contents of URL**.
7. URL field: `http://SERVER_ADDRESS:8000/save-pdf`
8. Tap **Show More**.
9. Set **Method** to **POST**.
10. Set **Request Body** to **Form** (not JSON — this sends multipart/form-data,
    which is required for file uploads).
11. Tap **Add new field** → set Type to **File**.
    - Key: `file`
    - Value: **Shortcut Input**
12. In the **Headers** section, tap **Add new field**:
    - Key: `Accept`
    - Value: `application/json`

    *If your server sets `MARGIN_TOKEN`*: add a second header — Key:
    `Authorization`, Value: `Bearer YOUR-TOKEN`.

**Step 3 — Parse the response**

13. Tap **Add Action** → **Get Dictionary from Input** → Input: **Contents of URL**.

14. Tap **Add Action** → **Get Dictionary Value** → Key: `summary`,
    Dictionary: **Dictionary** (pre-formatted by the server: filename on
    success, error message on failure).

**Step 4 — Notify**

15. Tap **Add Action** → **Show Notification**.
    - Title: `Margin`
    - Body: tap the variable icon → pick **Dictionary Value**.

16. Tap **Done**.

---

## Using the shortcuts

| Share source | What to share | Which shortcut appears |
|---|---|---|
| Safari | current page URL | Save to Margin |
| NetNewsWire / Unread | article link | Save to Margin |
| Files app | a PDF file | Save PDF to Margin |
| Safari | a PDF opened in browser | Save PDF to Margin |

---

## Icons and the Home Screen

**Shortcut icon inside the Shortcuts app / Share Sheet:** iOS only allows a
glyph + background color here, not custom images — tap the shortcut's icon
while editing to pick them (the bookmark glyph on a red background is a
reasonable match for Margin).

**Shortcut on the Home Screen with the real Margin icon:** custom images
*are* allowed there. First save the icon to Photos: open
`http://SERVER_ADDRESS:8000/static/icon-512.png` in Safari, long-press the
image → **Add to Photos**. Then: Shortcuts → ⋯ on the shortcut → **Add to
Home Screen** → tap the icon thumbnail → **Choose Photo** → select it.

**The Margin queue as a Home-Screen app:** open
`http://SERVER_ADDRESS:8000/` in Safari → Share → **Add to Home Screen**.
It installs with the Margin icon and opens full-screen like an app. Note:
the installed app has its own cookie storage, so if the server uses
`MARGIN_TOKEN` you'll see the token prompt on first launch — enter it once
and it's remembered.

---

## Troubleshooting

**"Get Dictionary from Input" still fails**
The server returned something other than JSON (an error page, or no response).
Check the server logs:

```bash
journalctl -u margin -n 30        # Ubuntu (systemd)
tail -30 server.log               # macOS (in the app directory, when launchd-run)
```

Also verify the server is running:
```bash
curl http://SERVER_ADDRESS:8000/health
```

**Debugging with `/echo`**
The server has a debug endpoint that returns whatever it received. Temporarily
change the shortcut URL from `/save` to `/echo`, run the shortcut, and inspect
the notification — it will show the exact request the server got. Swap back to
`/save` once confirmed.

---

## Appendix — standalone shortcuts (no server, Mathpix direct)

These call the Mathpix API directly from iOS and write the result to iCloud
Drive; no Margin server involved. They exist as a fallback and are weaker than
the server pipeline (OCR-based URL conversion, 100 s PDF timeout). Credentials
are stored as plain text inside the shortcut — do not share the shortcut file.

### Shared credential variables (add these at the top of both shortcuts)

1. **Add Action** → **Text** → paste your Mathpix `app_id` → tap the result
   variable name and rename it `MX_ID`.
2. **Add Action** → **Text** → paste your Mathpix `app_key` → rename result `MX_KEY`.
3. **Add Action** → **Text** → type `ReadLater/inbox` → rename result `INBOX_FOLDER`.

### "Save to Margin (no server)" — URLs

Share Sheet Types: **URLs only**

After the three credential actions:

4. **Add Action** → **Get Contents of URL**
   - URL: **Shortcut Input**
   - Method: **GET**
   - Rename result: `PAGE_HTML`

5. **Add Action** → **Get Contents of URL** *(second action — calls Mathpix)*
   - URL: `https://api.mathpix.com/v3/text`
   - Method: **POST**
   - **Headers** section — add three fields:
     - Key `app_id` → Value **MX_ID**
     - Key `app_key` → Value **MX_KEY**
     - Key `Accept` → Value `application/json`
   - **Body** section — set type to **JSON**, add fields:
     - Key `src` → Value **PAGE_HTML**
     - Key `formats` → Value `["mmd"]`
     - Key `math_inline_delimiters` → Value `["$","$"]`
     - Key `math_display_delimiters` → Value `["$$","$$"]`
   - Rename result: `MX_RESPONSE`

6. **Add Action** → **Get Dictionary from Input** → Input: **MX_RESPONSE** →
   rename result `MX_DICT`

7. **Add Action** → **Get Dictionary Value**
   - Key: `mmd`
   - Dictionary: **MX_DICT**
   - Rename result: `MD_BODY`

8. **Add Action** → **Format Date**
   - Date: **Current Date**
   - Format: **Custom** → `yyyy-MM-dd`
   - Rename result: `TODAY`

9. **Add Action** → **Text** → enter exactly:
   ```
   ---
   source_url: [Shortcut Input]
   date_saved: [TODAY]
   tags: [readlater, math]
   ---

   [MD_BODY]
   ```
   Tap each `[placeholder]` and replace it with the matching variable.
   Rename result: `FILE_CONTENT`

10. **Add Action** → **Save File**
    - Storage: **iCloud Drive**
    - Tap the path field → navigate to **ReadLater/inbox** (create it if it does
      not exist)
    - File name: tap the field → **TODAY** + `-article.md`
    - Ask where to save: **Off**

11. **Add Action** → **Show Notification** → Body: `Saved to iCloud Drive`

12. Tap **Done**.

### "Save PDF to Margin (no server)" — PDFs

Share Sheet Types: **PDFs only**

After the three credential actions:

4. **Add Action** → **Get Contents of URL** *(upload PDF to Mathpix)*
   - URL: `https://api.mathpix.com/v3/pdf`
   - Method: **POST**
   - **Headers** section:
     - Key `app_id` → Value **MX_ID**
     - Key `app_key` → Value **MX_KEY**
     - Key `Accept` → Value `application/json`
   - **Body** section — set type to **Form**, add two fields:
     - Type **File**, Key `file` → Value **Shortcut Input**
     - Type **Text**, Key `options_json` → Value:
       `{"conversion_formats":{"mmd":true},"math_inline_delimiters":["$","$"],"math_display_delimiters":["$$","$$"]}`
   - Rename result: `UPLOAD_RESP`

5. **Add Action** → **Get Dictionary from Input** → Input: **UPLOAD_RESP** →
   rename result `UPLOAD_DICT`

6. **Add Action** → **Get Dictionary Value**
   - Key: `pdf_id`
   - Dictionary: **UPLOAD_DICT**
   - Rename result: `PDF_ID`

7. **Add Action** → **Repeat** → Count: `20`

8. *(inside the Repeat block)* **Add Action** → **Wait** → `5` seconds

9. *(inside the Repeat block)* **Add Action** → **Get Contents of URL**
   - URL: tap field → type `https://api.mathpix.com/v3/pdf/` → tap variable icon →
     **PDF_ID**
   - Method: **GET**
   - **Headers**: `app_id` → **MX_ID**, `app_key` → **MX_KEY**,
     `Accept` → `application/json`
   - Rename result: `STATUS_RESP`

10. *(inside)* **Add Action** → **Get Dictionary from Input** → Input: **STATUS_RESP**
    → rename result `STATUS_DICT`

11. *(inside)* **Add Action** → **Get Dictionary Value** → Key: `status`,
    Dictionary: **STATUS_DICT** → rename result `PDF_STATUS`

12. *(inside)* **Add Action** → **If**
    - Input: **PDF_STATUS**
    - Condition: **is**
    - Value: `completed`
    - *(inside the If block)* **Add Action** → **Exit Repeat**
    - Tap **End If**

13. Tap **End Repeat**

14. **Add Action** → **Get Contents of URL** *(download the finished Markdown)*
    - URL: `https://api.mathpix.com/v3/pdf/` + **PDF_ID** + `.mmd`
    - Method: **GET**
    - **Headers**: `app_id` → **MX_ID**, `app_key` → **MX_KEY**
    - Rename result: `MD_BODY`

15. **Add Action** → **Format Date** → format `yyyy-MM-dd` → rename `TODAY`

16. **Add Action** → **Text**:
    ```
    ---
    date_saved: [TODAY]
    tags: [readlater, math]
    ---

    [MD_BODY]
    ```
    Replace each `[placeholder]` with its variable. Rename result: `FILE_CONTENT`

17. **Add Action** → **Save File**
    - Storage: **iCloud Drive**
    - Navigate to **ReadLater/inbox**
    - File name: **TODAY** + `-document.md`
    - Ask where to save: **Off**

18. **Add Action** → **Show Notification** → Body: `Saved to iCloud Drive`

19. Tap **Done**.

> **Limitation:** The polling loop runs for up to 100 s (20 × 5 s). PDFs over
> ~50 pages may exceed this — use the server shortcut for those.

---

## Quick reference — server endpoints

| Endpoint | Method | Body | Returns |
|---|---|---|---|
| `POST /save` | POST | JSON `{"url":"…"}` (formats optional, default `pdf,md,tex`) | `{"status":"ok","files":[…],"summary":"..."}` |
| `POST /save-url` | POST | JSON `{"url":"https://..."}` (Markdown only, legacy) | `{"status":"ok","files":[…],"summary":"..."}` |
| `POST /save-pdf` | POST | multipart form, field `file` (keeps PDF + OCRs to md/tex) | `{"status":"ok","files":[…],"summary":"..."}` |
| `GET /save-page` | GET | query `?url=…&formats=pdf,md` | HTML result page (desktop bookmarklet) |
| `POST /echo` | POST | anything | mirrors back headers + body |
| `GET /health` | GET | — | server status + config check |
