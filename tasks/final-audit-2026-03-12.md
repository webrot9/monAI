# monAI — Audit Finale Pre-Launch — 2026-03-12

## Executive Summary

Audit completo su 6 dimensioni: autonomia, capacità economica, bug, privacy/anonimato, auto-miglioramento, miglioramento progetti. 42.000+ LOC, 1.579 test (1.462 pass, 117 fail per dipendenza mancante).

---

## SCORECARD FINALE

| Dimensione | Score | Motivazione |
|-----------|-------|-------------|
| **Autonomia** | **8/10** | Orchestrator 10-fasi, auto-provisioning infrastruttura, daemon mode. -1 per KYC manuale, -1 per self-improvement quasi inerte |
| **Capacità Economica** | **7.5/10** | 6 payment provider reali, double-entry GL, sweep engine, tax estimation. -1 per RetoSwap incompleto, -0.5 per balance check Monero nell'ordine sbagliato, -1 per nessun test con soldi veri |
| **Bug** | **6/10** | 117 test falliscono (pytest-asyncio mancante), self-improve segna "deployed" prima del deploy effettivo, webhook server non ha shutdown graceful, action execution hardcoded per 4 tipi |
| **Privacy & Anonimato** | **6.5/10** | Tor + proxy chain + fingerprint randomization. -1 per IP leak a ipify.org, -1 per identità singola cross-platform, -0.5 per ALLOW_NO_PROXY env override, -0.5 per headless detection, -0.5 per WebGL non disabilitato |
| **Auto-miglioramento** | **4/10** | Il sistema genera miglioramenti ma il 90% sono medium/high-risk e non vengono mai deployati. Solo low-risk (rari) auto-deploy. Status marcato "deployed" PRIMA del deploy. Nessun feedback loop |
| **Miglioramento Progetti** | **7/10** | Coder agent con TDD autonomo, engineering team, code review, browser learner. -1 per nessuna CI/CD pipeline, -1 per workflow engine con keyword matching troppo naive, -1 per nessun A/B test framework |
| **TOTALE** | **6.5/10** | |

---

## DETTAGLIO PER DIMENSIONE

### 1. AUTONOMIA — 8/10

**Cosa funziona (bene):**
- Orchestrator ciclo 10-fasi con 20+ sub-fasi, intervallo 5 min
- Auto-setup completo: Tor, Monero wallet, Ollama, config
- Daemon mode con shutdown graceful (SIGINT/SIGTERM)
- 14 strategy agents con browser automation reale
- Payment collection + sweep autonomi
- Telegram reporting automatico
- Budget enforcement per ciclo (€5 cap)

**Cosa non funziona:**
- `_execute_action()` gestisce solo 4 tipi di azione (discover, rebalance, follow_up, marketing) — il resto viene "queued" senza esecuzione
- Self-improvement genera miglioramenti ma non li applica (90% medium/high-risk)
- Errori silenziosi su exchange rates, payment sweep, Telegram alerts
- Webhook server non ha meccanismo di stop (thread zombie)
- Auto-setup continua anche se Tor/Monero falliscono (opera senza anonimato)

### 2. CAPACITA ECONOMICA — 7.5/10

**Cosa funziona (bene):**
- Double-entry GL completo con chart of accounts, P&L, balance sheet
- 6 payment provider reali (Stripe, BTCPay, Monero, Gumroad, LemonSqueezy, Ko-fi)
- Webhook signature verification obbligatoria su tutti i provider
- Spending guard con cap giornaliero/per-transazione/per-strategy
- Tax estimation (forfettario IT + US federal)
- Invoice generation (HTML + PDF)
- Exchange rate service (ECB + CoinGecko) con caching

**Cosa non funziona:**
- RetoSwap implementation incompleta (nessun trade reale)
- Monero balance check DOPO l'inizio del sweep (ordine sbagliato)
- Invoice number generation non thread-safe (COUNT(*) può duplicare)
- GL usa REAL (float) per importi — rischio drift su grandi volumi
- Nessun test end-to-end con soldi veri
- Risk manager solo reporting, nessuna azione automatica

### 3. BUG — 6/10

**Critici (117 test failures):**
- `pytest-asyncio` mancante da pyproject.toml — TUTTI i test async falliscono
- Self-improve segna status "deployed" PRIMA di chiamare il metodo di deploy
- Webhook server avviato in background thread senza meccanismo di stop
- `get_real_ip()` fa connessione diretta (non proxied) a ipify.org

**Alti:**
- Division by zero in `get_metric_trend()` se `len(values) <= 1`
- `_fix_code()` ritorna dict senza validazione che sia codice valido
- Browser timeout non fa cleanup dello stato
- Rate limiter webhook senza lock (race condition su contatori)

**Medi:**
- Keyword matching nel router troppo naive ("code" matcha "decode")
- Parameter JSON parsing brittle in self_improve
- Condition lambda in pipelines cattura closure vars mutabili

### 4. PRIVACY & ANONIMATO — 6.5/10

**Cosa funziona (bene):**
- Tor proxy chain con fallback (Tor → residential → datacenter)
- WebRTC disabilitato
- Canvas fingerprinting difeso con pixel noise
- User-agent rotation (8 configurazioni)
- Browser fingerprint randomizzato (viewport, timezone, locale)
- Metadata stripping (EXIF, PDF)
- IP hasher prima del logging (SHA256)
- Credenziali criptate in DB (Fernet)

**Cosa non funziona:**
- **IP LEAK CRITICO**: `get_real_ip()` fa UNA connessione DIRETTA a ipify.org — il vero IP del creatore viene loggato
- **IDENTITA SINGOLA**: stesso company name/username su TUTTE le piattaforme — correlazione ovvia
- **ALLOW_NO_PROXY**: env var `MONAI_ALLOW_NO_PROXY=1` disabilita tutto il proxy silenziosamente
- **Headless detection**: nessuna prevenzione (chrome.runtime mancante, window properties diverse)
- **WebGL non disabilitato**: GPU fingerprint espone hardware/OS
- **Timezone mismatch**: timezone random non correlato con viewport/locale
- **Webhook server su 0.0.0.0**: espone IP reale se non dietro Tor/reverse proxy
- **Phone number riutilizzato**: stessi numeri per più account = correlazione

### 5. AUTO-MIGLIORAMENTO — 4/10

**Cosa funziona:**
- Metrics tracking per agente
- Trend analysis (improving/declining/stable)
- Generazione proposte miglioramento via LLM
- Risk assessment (high/medium/low)

**Cosa NON funziona (il cuore del problema):**
- Il 90%+ dei miglioramenti generati sono "medium" o "high" risk
- Solo "low" risk viene auto-deployato (quasi mai)
- Status marcato "deployed" PRIMA del deploy effettivo — se il deploy fallisce, risulta comunque deployato
- Nessun feedback loop: non misura se il miglioramento ha funzionato
- `generate_improvements()` richiede data_richness == "good" — agenti con pochi dati non migliorano MAI
- SharedMemory può non essere disponibile → improvement perso

### 6. MIGLIORAMENTO PROGETTI — 7/10

**Cosa funziona:**
- Coder agent con TDD: genera codice → genera test → esegue → fix automatico
- Engineering team (tech lead + engineers)
- Code review capability
- Workflow engine con DAG, retry, fan-out/fan-in
- Pipeline pre-build per 8 workflow comuni
- Task router con learning (proficiency updates)

**Cosa non funziona:**
- Nessuna CI/CD pipeline automatica
- Keyword matching nel router troppo broad
- Nessun A/B testing framework
- Circular dependency detection senza suggerimento fix
- `**ctx` spread in action calls assume che l'agent accetti tutti i kwargs

---

## ISSUES DA FIXARE ORA (Pre-Launch)

### Priorità CRITICA (bloccanti per il lancio)
1. **pytest-asyncio mancante** — 117 test falliscono, impossibile verificare nulla
2. **Self-improve deployment lifecycle** — status "deployed" prima del deploy effettivo
3. **Webhook server shutdown** — nessun meccanismo di stop, thread zombie
4. **IP leak ipify.org** — connessione diretta espone IP reale
5. **Identità singola cross-platform** — correlazione account ovvia
6. **ALLOW_NO_PROXY override** — disabilita proxy via env var

### Priorità ALTA (da fare settimana 1)
7. **Headless browser detection** — aggiungi flag anti-detection
8. **WebGL disable** — previeni GPU fingerprinting
9. **Division by zero in self_improve** — `get_metric_trend()`
10. **Monero balance check order** — controllare prima di iniziare sweep

---

## PIANO D'AZIONE IMMEDIATO

Fixing items 1-8 now. Items 9-10 sono quick fixes inclusi.
