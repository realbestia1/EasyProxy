# CF Worker AI — OCR Backend per uprot Captcha

EasyProxy risolve il captcha 3-cifre di uprot.net con `ddddocr` + `tesseract`
in locale. Su captcha rumorosi il rate combinato è ~50-65%. Aggiungendo un
**Cloudflare Workers AI** come 3° backend (Llama 4 Scout / Gemma 3 12B
multimodal) il rate sale a **~90%**.

L'integrazione è **opt-in** via 2 env vars: se non configurate, EasyProxy
funziona esattamente come prima (zero regression).

---

## Setup (5 minuti, gratis)

### 1. Account Cloudflare

Se non l'hai già: registrati gratis su https://dash.cloudflare.com.

### 2. Crea il Worker

- Dashboard → **Workers & Pages** → **Create application**
- Nome: `easyproxy-ocr` (o quello che vuoi)
- Click **Deploy**

### 3. Incolla il codice

Apri il Worker → **Edit code** e incolla:

```javascript
// easyproxy-ocr Worker — Captcha OCR con CF Workers AI
// Endpoint: POST /?ocr=1[&digits=3]
//   Headers: x-worker-auth: <AUTH_TOKEN>, content-type: image/png
//   Body: PNG bytes

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    // Auth check
    const authToken = (env.AUTH_TOKEN || '').trim();
    if (authToken) {
      const provided = (
        request.headers.get('x-worker-auth') ||
        url.searchParams.get('auth') ||
        ''
      ).trim();
      if (provided !== authToken) {
        return _json({ error: 'Unauthorized' }, 401);
      }
    }

    if (url.searchParams.get('ocr') !== '1') {
      return _json({ error: 'POST /?ocr=1 with PNG body' }, 400);
    }
    if (request.method !== 'POST') {
      return _json({ error: 'POST required' }, 405);
    }
    if (!env?.AI) {
      return _json({ error: 'AI binding not configured' }, 500);
    }

    try {
      const buf = new Uint8Array(await request.arrayBuffer());
      if (buf.length === 0 || buf.length > 1024 * 1024) {
        return _json({ error: `Invalid PNG size: ${buf.length}` }, 400);
      }
      const expected = parseInt(url.searchParams.get('digits') || '3', 10);
      const digits = await _aiOcrDigits(env, buf, expected);
      if (!digits) return _json({ error: 'OCR failed' }, 422);
      return _json({ digits, method: 'ai', expected });
    } catch (e) {
      return _json({ error: `OCR error: ${e.message}` }, 500);
    }
  },
};

async function _aiOcrDigits(env, imageBytes, expectedDigits = 3) {
  const allAnswers = [];
  const b64 = btoa(String.fromCharCode.apply(null, imageBytes));

  const prompts = [
    `The image shows ${expectedDigits} digits in a captcha. Reply with ONLY the ${expectedDigits} digits, nothing else.`,
    `Read the ${expectedDigits} numbers (0-9) in this image left to right. Output ONLY ${expectedDigits} digits, no other characters.`,
    `Extract the ${expectedDigits} digits from this captcha. Reply with the ${expectedDigits} digits only.`,
  ];

  const runOne = async (prompt) => {
    // Try multiple vision models
    for (const model of [
      '@cf/meta/llama-4-scout-17b-16e-instruct',
      '@cf/google/gemma-3-12b-it',
      '@cf/meta/llama-3.2-11b-vision-instruct',
    ]) {
      try {
        const resp = await env.AI.run(model, {
          messages: [{
            role: 'user',
            content: [
              { type: 'image_url', image_url: { url: `data:image/png;base64,${b64}` } },
              { type: 'text', text: prompt },
            ],
          }],
          max_tokens: 20,
        });
        const text = (resp?.response || '').replace(/[^0-9]/g, '');
        if (text) return text;
      } catch { /* try next */ }
    }
    return '';
  };

  const results = await Promise.allSettled(prompts.map(runOne));
  for (const r of results) {
    if (r.status === 'fulfilled' && r.value) allAnswers.push(r.value);
  }
  if (!allAnswers.length) return null;

  // Majority vote on answers with exactly expectedDigits
  const exact = allAnswers.filter(a => a.length === expectedDigits);
  if (exact.length) {
    const freq = {};
    for (const a of exact) freq[a] = (freq[a] || 0) + 1;
    return Object.entries(freq).sort((a, b) => b[1] - a[1])[0][0];
  }
  return null;
}

function _json(obj, status = 200) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: {
      'Content-Type': 'application/json; charset=utf-8',
      'Access-Control-Allow-Origin': '*',
    },
  });
}
```

Click **Deploy**.

### 4. Abilita Workers AI

- Worker → **Settings** → **Bindings** → **Add binding** → **Workers AI**
- Variable name: `AI`
- Save & redeploy

### 5. Imposta AUTH_TOKEN (raccomandato)

- Worker → **Settings** → **Variables and Secrets** → **Add variable**
- Name: `AUTH_TOKEN`
- Value: una stringa segreta a tua scelta (es. `mysecret123`)
- Type: Secret
- Save & redeploy

### 6. Configura EasyProxy

Aggiungi al docker-compose (o env del tuo deploy EasyProxy):

```yaml
environment:
  - CF_WORKER_OCR_URL=https://easyproxy-ocr.tuoaccount.workers.dev
  - CF_WORKER_OCR_AUTH=mysecret123
```

Restart EasyProxy. Done.

---

## Test rapido

```bash
# Sostituisci URL e AUTH_TOKEN con i tuoi
curl -X POST \
  -H "x-worker-auth: mysecret123" \
  -H "content-type: image/png" \
  --data-binary @captcha.png \
  "https://easyproxy-ocr.tuoaccount.workers.dev/?ocr=1&digits=3"

# Risposta attesa:
# {"digits":"536","method":"ai","expected":3}
```

---

## Limiti CF Workers (free tier)

| Risorsa | Limite gratis | Note |
|---|---|---|
| Request/giorno | 100.000 | abbondante per uso personale |
| Workers AI (Llama/Gemma) | ~10.000 neuroni/giorno | ~3000 captcha/giorno |
| KV (non usato qui) | 100k read, 1k write | n/a |

Per ~3000 captcha solve/giorno il piano free basta. Oltre, $5/mese piano paid.

---

## Comportamento se Worker offline / AI fallisce

EasyProxy fa fallback automatico a `ddddocr` + `tesseract` (~50% rate). Mai
crash, mai blocco. Il CF Worker OCR è puramente additive.
