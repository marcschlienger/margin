# Apple Shortcuts Setup

> **Why two shortcuts instead of one?**
> Shortcuts cannot reliably branch on whether the input is a URL vs a file — the
> `If … is URL` condition is inconsistent across share sources (Safari, RSS readers,
> Files app). The solution is two focused shortcuts, each accepting only the type it
> handles. iOS shows each shortcut only when the Share Sheet input matches its
> declared type, so the right one always appears.

Four shortcuts are described here (Option B recommended):

| Name | Accepts | Requires server? |
|---|---|---|
| **Math Inbox — URL** | URLs | Yes (Option B) |
| **Math Inbox — PDF** | PDF files | Yes (Option B) |
| **Math Inbox — URL (standalone)** | URLs | No (Option A) |
| **Math Inbox — PDF (standalone)** | PDF files | No (Option A) |

---

## Option B — Two companion shortcuts  *(recommended)*

The shortcuts POST to the Mac server and show a notification. All conversion logic
stays server-side.

### Prerequisites

- The Mac server is running: `launchctl start com.marc.math-readlater`
- Confirm it is up: `curl http://localhost:8000/health`
- Find your Mac's IP for the shortcut URL:
  - Same Wi-Fi → System Settings → Network → your interface → IP address,
    e.g. `http://192.168.1.42:8000`
  - Away from home → install [Tailscale](https://tailscale.com) on both devices;
    use the Tailscale IP shown in the menu bar app, e.g. `http://100.x.y.z:8000`

---

### Shortcut 1 of 2 — "Math Inbox — URL"

**Step 1 — Create and configure**

1. Open the **Shortcuts** app → tap **+** (top right).
2. Tap the title field at the top → type **Math Inbox — URL** → tap **Done**.
3. Tap **ⓘ** (bottom right) → turn on **Show in Share Sheet**.
4. Tap **Share Sheet Types** → deselect everything except **URLs** → tap **Done**.
5. Tap **Done** to close the details panel.

**Step 2 — Add the request action**

6. Tap **Add Action** → search for **Get Contents of URL** → tap it.
7. Tap the blue **URL** field in the action → type your server address:
   `http://YOUR_MAC_IP:8000/save-url`
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

**Step 3 — Parse the response**

13. Tap **Add Action** → search for **Get Dictionary from Input** → tap it.
    - Input: tap the field → select **Contents of URL**
    - This explicitly parses the JSON response into a Dictionary. Without this
      step, Shortcuts sometimes treats a JSON response as plain Text and the next
      step fails with "couldn't convert from Text to Dictionary".

14. Tap **Add Action** → **Get Dictionary Value** → Key: `filename`,
    Dictionary: **Dictionary**. The result is a variable named **Dictionary Value**.

15. Tap **Add Action** → **Get Dictionary Value** → Key: `message`,
    Dictionary: **Dictionary**. The result is named **Dictionary Value 2**
    (Shortcuts auto-numbers identical action outputs — there's no need to
    rename anything).

**Step 4 — Notify**

16. Tap **Add Action** → search for **Show Notification** → tap it.
    - Title: `Math Inbox`
    - Body: tap the field → type `Saved: ` → tap the variable icon → pick
      **Dictionary Value** → type ` ` → tap the variable icon again → pick
      **Dictionary Value 2**.
    - On success the second value is empty, so the notification reads
      `Saved: 2026-04-26-title.md`. On error the first is empty, so you see
      the error message from the server instead.

17. Tap **Done** (top right).

---

### Shortcut 2 of 2 — "Math Inbox — PDF"

**Step 1 — Create and configure**

1. Open **Shortcuts** → tap **+**.
2. Name it **Math Inbox — PDF**.
3. Tap **ⓘ** → turn on **Show in Share Sheet**.
4. Tap **Share Sheet Types** → deselect everything except **PDFs** → tap **Done**.
5. Tap **Done** to close the details panel.

**Step 2 — Add the request action**

6. Tap **Add Action** → **Get Contents of URL**.
7. URL field: `http://YOUR_MAC_IP:8000/save-pdf`
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

**Step 3 — Parse the response**
ls

1. Tap **Add Action** → **Get Dictionary from Input** → Input: **Contents of URL**.

14. Tap **Add Action** → **Get Dictionary Value** → Key: `filename`,
    Dictionary: **Dictionary**. The result is **Dictionary Value**.

15. Tap **Add Action** → **Get Dictionary Value** → Key: `message`,
    Dictionary: **Dictionary**. The result is **Dictionary Value 2**.

**Step 4 — Notify**

16. Tap **Add Action** → **Show Notification**.
    - Title: `Math Inbox`
    - Body: `Saved: ` + **Dictionary Value** + ` ` + **Dictionary Value 2**.
    - On success you see the filename; on error you see the server's error message.

17. Tap **Done**.

---

### Using the shortcuts

| Share source | What to share | Which shortcut appears |
|---|---|---|
| Safari | current page URL | Math Inbox — URL |
| NetNewsWire / Unread | article link | Math Inbox — URL |
| Files app | a PDF file | Math Inbox — PDF |
| Safari | a PDF opened in browser | Math Inbox — PDF |

---

### Troubleshooting

**"Get Dictionary from Input" still fails**
The server returned something other than JSON (an error page, or no response).
Check the server log on your Mac:
```bash
tail -30 ~/Documents/math-readlater/server.log
```
Also verify the server is running:
```bash
curl http://localhost:8000/health
```

**Debugging with `/echo`**
The server has a debug endpoint that returns whatever it received. Temporarily
change the shortcut URL from `/save-url` to `/echo`, run the shortcut, and inspect
the notification — it will show the exact request the server got. Swap back to
`/save-url` once confirmed.

---

## Option A — Two standalone shortcuts  *(no server required)*

These call the Mathpix API directly from iOS. Credentials are stored as plain text
inside the shortcut — do not share the shortcut file.

### Shared credential variables (add these at the top of both shortcuts)

1. **Add Action** → **Text** → paste your Mathpix `app_id` → tap the result
   variable name and rename it `MX_ID`.
2. **Add Action** → **Text** → paste your Mathpix `app_key` → rename result `MX_KEY`.
3. **Add Action** → **Text** → type `ReadLater/inbox` → rename result `INBOX_FOLDER`.

---

### Shortcut A1 — "Math Inbox — URL (standalone)"

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

---

### Shortcut A2 — "Math Inbox — PDF (standalone)"

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
> ~50 pages may exceed this — use the Option B server shortcut for those.

---

## Quick reference — server endpoints

| Endpoint | Method | Body | Returns |
|---|---|---|---|
| `POST /save-url` | POST | JSON `{"url":"https://..."}` | `{"status":"ok","filename":"...","title":"..."}` |
| `POST /save-pdf` | POST | multipart form, field `file` | `{"status":"ok","filename":"...","title":"..."}` |
| `POST /echo` | POST | anything | mirrors back headers + body |
| `GET /health` | GET | — | server status + config check |
