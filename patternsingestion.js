import { useState, useRef, useCallback } from "react";

// ── Paleta y estilos globales ──────────────────────────────────────────────
const COLORS = {
  bg:       "#0a0e1a",
  surface:  "#111827",
  border:   "#1e2d40",
  accent:   "#00d4ff",
  green:    "#00ff88",
  red:      "#ff4466",
  yellow:   "#ffd700",
  muted:    "#4a5568",
  text:     "#e2e8f0",
  textDim:  "#718096",
};

const css = {
  app: {
    minHeight: "100vh",
    background: COLORS.bg,
    fontFamily: "'JetBrains Mono', 'Fira Code', monospace",
    color: COLORS.text,
    padding: "24px",
  },
  header: {
    borderBottom: `1px solid ${COLORS.border}`,
    paddingBottom: "20px",
    marginBottom: "28px",
  },
  title: {
    fontSize: "22px",
    fontWeight: 700,
    color: COLORS.accent,
    letterSpacing: "0.08em",
    textTransform: "uppercase",
    margin: 0,
  },
  subtitle: {
    fontSize: "12px",
    color: COLORS.textDim,
    marginTop: "4px",
    letterSpacing: "0.05em",
  },
  grid: {
    display: "grid",
    gridTemplateColumns: "1fr 1fr",
    gap: "20px",
  },
  card: {
    background: COLORS.surface,
    border: `1px solid ${COLORS.border}`,
    borderRadius: "8px",
    padding: "20px",
  },
  cardTitle: {
    fontSize: "11px",
    letterSpacing: "0.12em",
    color: COLORS.textDim,
    textTransform: "uppercase",
    marginBottom: "14px",
    fontWeight: 600,
  },
  dropzone: (dragging, hasImage) => ({
    border: `2px dashed ${dragging ? COLORS.accent : hasImage ? COLORS.green : COLORS.border}`,
    borderRadius: "8px",
    minHeight: "180px",
    display: "flex",
    flexDirection: "column",
    alignItems: "center",
    justifyContent: "center",
    cursor: "pointer",
    transition: "all 0.2s",
    background: dragging ? "rgba(0,212,255,0.04)" : "transparent",
    position: "relative",
    overflow: "hidden",
  }),
  btn: (variant = "primary") => ({
    padding: "10px 20px",
    borderRadius: "6px",
    border: "none",
    cursor: "pointer",
    fontSize: "12px",
    fontFamily: "inherit",
    fontWeight: 700,
    letterSpacing: "0.08em",
    textTransform: "uppercase",
    transition: "all 0.15s",
    background: variant === "primary"   ? COLORS.accent
              : variant === "success"   ? COLORS.green
              : variant === "danger"    ? COLORS.red
              : variant === "secondary" ? COLORS.border
              : COLORS.muted,
    color: variant === "primary" || variant === "success" || variant === "danger"
           ? COLORS.bg : COLORS.text,
  }),
  input: {
    width: "100%",
    background: COLORS.bg,
    border: `1px solid ${COLORS.border}`,
    borderRadius: "6px",
    padding: "8px 12px",
    color: COLORS.text,
    fontFamily: "inherit",
    fontSize: "13px",
    boxSizing: "border-box",
  },
  select: {
    width: "100%",
    background: COLORS.bg,
    border: `1px solid ${COLORS.border}`,
    borderRadius: "6px",
    padding: "8px 12px",
    color: COLORS.text,
    fontFamily: "inherit",
    fontSize: "13px",
  },
  label: {
    fontSize: "11px",
    color: COLORS.textDim,
    letterSpacing: "0.06em",
    display: "block",
    marginBottom: "6px",
    textTransform: "uppercase",
  },
  field: { marginBottom: "14px" },
  tag: (color) => ({
    display: "inline-block",
    padding: "3px 10px",
    borderRadius: "4px",
    fontSize: "11px",
    fontWeight: 700,
    letterSpacing: "0.06em",
    background: color + "22",
    color: color,
    border: `1px solid ${color}44`,
    marginRight: "6px",
    marginBottom: "4px",
  }),
  row: {
    display: "flex",
    gap: "10px",
    alignItems: "center",
    marginBottom: "10px",
  },
  dbRow: {
    display: "flex",
    justifyContent: "space-between",
    alignItems: "center",
    padding: "10px 14px",
    borderBottom: `1px solid ${COLORS.border}`,
    fontSize: "12px",
  },
  toast: (type) => ({
    position: "fixed",
    bottom: "24px",
    right: "24px",
    background: type === "success" ? COLORS.green : type === "error" ? COLORS.red : COLORS.accent,
    color: COLORS.bg,
    padding: "12px 20px",
    borderRadius: "6px",
    fontSize: "12px",
    fontWeight: 700,
    letterSpacing: "0.06em",
    zIndex: 9999,
    animation: "fadeIn 0.2s ease",
  }),
};

// ── Campos del formulario de confirmación ────────────────────────────────
const SIGNAL_TYPES = ["LONG_BREAKOUT","LONG_REVERSAL","SHORT_BREAKOUT","SHORT_REVERSAL"];
const TRENDS       = ["BULLISH","BEARISH","SIDEWAYS"];
const RISK_LEVELS  = ["BAJO","MEDIO","ALTO"];
const ENTRY_TYPES  = ["ENTRADA_PRINCIPAL","RECOMPRA","ENTRADA_PIRAMIDE"];

const EMPTY_FORM = {
  symbol:      "",
  signal_type: "LONG_REVERSAL",
  trend:       "BULLISH",
  risk_level:  "BAJO",
  entry_type:  "ENTRADA_PRINCIPAL",
  ema_context: "",
  bb_context:  "",
  notes:       "",
  entry_zone:  "",
  sl_zone:     "",
  tp_zone:     "",
};

// ── Llamada a Claude Vision API ──────────────────────────────────────────
async function analyzeChartWithClaude(base64Image, mimeType) {
  const response = await fetch("https://api.anthropic.com/v1/messages", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      model: "claude-sonnet-4-20250514",
      max_tokens: 1000,
      messages: [{
        role: "user",
        content: [
          {
            type: "image",
            source: { type: "base64", media_type: mimeType, data: base64Image }
          },
          {
            type: "text",
            text: `Analiza este chart de trading y extrae la información en JSON puro sin markdown ni backticks.
Responde SOLO con este JSON, sin texto adicional:
{
  "symbol": "símbolo detectado o vacío si no se ve",
  "signal_type": "LONG_BREAKOUT|LONG_REVERSAL|SHORT_BREAKOUT|SHORT_REVERSAL",
  "trend": "BULLISH|BEARISH|SIDEWAYS",
  "risk_level": "BAJO|MEDIO|ALTO",
  "entry_type": "ENTRADA_PRINCIPAL|RECOMPRA|ENTRADA_PIRAMIDE",
  "ema_context": "descripción de la posición de EMAs visibles (21/55/144/233)",
  "bb_context": "descripción de Bandas de Bollinger: squeeze, expansión, posición del precio",
  "entry_zone": "precio o zona aproximada de entrada si es visible",
  "sl_zone": "zona de stop loss si es visible",
  "tp_zone": "zona de take profit si es visible",
  "notes": "observaciones sobre velas, patrones, confluencias visibles"
}`
          }
        ]
      }]
    })
  });
  const data = await response.json();
  const text = data.content?.map(b => b.text || "").join("") || "";
  const clean = text.replace(/```json|```/g, "").trim();
  return JSON.parse(clean);
}

// ── Componente principal ─────────────────────────────────────────────────
export default function PatternIngestion() {
  const [dragging, setDragging]       = useState(false);
  const [imageFile, setImageFile]     = useState(null);
  const [imagePreview, setImagePreview] = useState(null);
  const [imageBase64, setImageBase64] = useState(null);
  const [imageMime, setImageMime]     = useState(null);
  const [analyzing, setAnalyzing]     = useState(false);
  const [form, setForm]               = useState(EMPTY_FORM);
  const [analyzed, setAnalyzed]       = useState(false);
  const [patterns, setPatterns]       = useState([]);
  const [toast, setToast]             = useState(null);
  const [activeTab, setActiveTab]     = useState("ingest"); // ingest | db
  const fileRef = useRef();

  const showToast = (msg, type = "success") => {
    setToast({ msg, type });
    setTimeout(() => setToast(null), 3000);
  };

  // Procesar imagen seleccionada
  const processImage = useCallback((file) => {
    if (!file || !file.type.startsWith("image/")) {
      showToast("Solo se aceptan imágenes", "error");
      return;
    }
    setImageFile(file);
    setImagePreview(URL.createObjectURL(file));
    setAnalyzed(false);
    setForm(EMPTY_FORM);

    const reader = new FileReader();
    reader.onload = (e) => {
      const b64 = e.target.result.split(",")[1];
      setImageBase64(b64);
      setImageMime(file.type);
    };
    reader.readAsDataURL(file);
  }, []);

  const onDrop = (e) => {
    e.preventDefault();
    setDragging(false);
    const file = e.dataTransfer.files[0];
    processImage(file);
  };

  // Análisis con Claude Vision
  const handleAnalyze = async () => {
    if (!imageBase64) return;
    setAnalyzing(true);
    try {
      const result = await analyzeChartWithClaude(imageBase64, imageMime);
      setForm(prev => ({ ...EMPTY_FORM, ...result }));
      setAnalyzed(true);
      showToast("✓ Análisis completado — revisa y confirma");
    } catch (err) {
      showToast("Error analizando imagen: " + err.message, "error");
    } finally {
      setAnalyzing(false);
    }
  };

  // Guardar patrón en BD local (array en memoria / localStorage export)
  const handleSave = () => {
    const pattern = {
      id:         Date.now(),
      ...form,
      timestamp:  new Date().toISOString(),
      image_name: imageFile?.name || "unknown",
    };
    setPatterns(prev => [pattern, ...prev]);
    showToast("✓ Patrón guardado en la base de datos");
    setImageFile(null);
    setImagePreview(null);
    setImageBase64(null);
    setForm(EMPTY_FORM);
    setAnalyzed(false);
  };

  const handleDiscard = () => {
    setImageFile(null);
    setImagePreview(null);
    setImageBase64(null);
    setForm(EMPTY_FORM);
    setAnalyzed(false);
  };

  // Exportar BD como JSON
  const handleExport = () => {
    const blob = new Blob([JSON.stringify(patterns, null, 2)], { type: "application/json" });
    const url  = URL.createObjectURL(blob);
    const a    = document.createElement("a");
    a.href     = url;
    a.download = `patterns_${Date.now()}.json`;
    a.click();
    showToast("✓ BD exportada como JSON");
  };

  const F = (k) => (v) => setForm(p => ({ ...p, [k]: typeof v === "string" ? v : v.target.value }));

  const signalColor = (s) =>
    s?.includes("LONG") ? COLORS.green : s?.includes("SHORT") ? COLORS.red : COLORS.yellow;

  return (
    <div style={css.app}>
      <style>{`
        @keyframes fadeIn { from { opacity:0; transform:translateY(8px) } to { opacity:1; transform:none } }
        @keyframes spin { to { transform: rotate(360deg) } }
        * { box-sizing: border-box; }
        ::-webkit-scrollbar { width: 4px; }
        ::-webkit-scrollbar-track { background: ${COLORS.bg}; }
        ::-webkit-scrollbar-thumb { background: ${COLORS.border}; border-radius: 2px; }
        textarea { resize: vertical; }
      `}</style>

      {/* Header */}
      <div style={css.header}>
        <h1 style={css.title}>⚡ Pattern Intelligence DB</h1>
        <p style={css.subtitle}>ÚNICO STRATEGY — Ingesta de patrones por screenshot</p>
        <div style={{ display:"flex", gap:"12px", marginTop:"16px" }}>
          {["ingest","db"].map(tab => (
            <button key={tab} onClick={() => setActiveTab(tab)} style={{
              ...css.btn(activeTab === tab ? "primary" : "secondary"),
              opacity: activeTab === tab ? 1 : 0.6,
            }}>
              {tab === "ingest" ? "📸 Nuevo Patrón" : `🗄 Base de Datos (${patterns.length})`}
            </button>
          ))}
        </div>
      </div>

      {/* TAB: INGESTA */}
      {activeTab === "ingest" && (
        <div style={css.grid}>

          {/* Columna izquierda: Upload + Preview */}
          <div>
            <div style={css.card}>
              <div style={css.cardTitle}>1 — Sube tu screenshot</div>

              <div
                style={css.dropzone(dragging, !!imagePreview)}
                onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
                onDragLeave={() => setDragging(false)}
                onDrop={onDrop}
                onClick={() => fileRef.current?.click()}
              >
                {imagePreview ? (
                  <img src={imagePreview} alt="chart" style={{
                    width:"100%", height:"auto", borderRadius:"6px", display:"block"
                  }}/>
                ) : (
                  <>
                    <div style={{ fontSize:"36px", marginBottom:"10px" }}>📊</div>
                    <div style={{ fontSize:"13px", color: COLORS.textDim }}>
                      Arrastra tu screenshot aquí
                    </div>
                    <div style={{ fontSize:"11px", color: COLORS.muted, marginTop:"6px" }}>
                      o haz click para seleccionar
                    </div>
                  </>
                )}
              </div>
              <input
                ref={fileRef} type="file" accept="image/*"
                style={{ display:"none" }}
                onChange={(e) => processImage(e.target.files[0])}
              />

              {imageFile && !analyzed && (
                <button
                  onClick={handleAnalyze}
                  disabled={analyzing}
                  style={{ ...css.btn("primary"), width:"100%", marginTop:"12px" }}
                >
                  {analyzing ? (
                    <span>
                      <span style={{
                        display:"inline-block",
                        animation:"spin 0.8s linear infinite",
                        marginRight:"8px"
                      }}>⟳</span>
                      Analizando con Claude Vision...
                    </span>
                  ) : "🔍 Analizar con Claude Vision"}
                </button>
              )}

              {analyzed && (
                <div style={{
                  marginTop:"12px", padding:"10px 14px",
                  background:"rgba(0,255,136,0.08)",
                  border:`1px solid ${COLORS.green}44`,
                  borderRadius:"6px", fontSize:"12px", color: COLORS.green,
                }}>
                  ✓ Análisis listo — revisa los campos y confirma
                </div>
              )}
            </div>
          </div>

          {/* Columna derecha: Formulario de confirmación */}
          <div>
            <div style={css.card}>
              <div style={css.cardTitle}>2 — Confirma o corrige los datos</div>

              <div style={css.field}>
                <label style={css.label}>Símbolo</label>
                <input style={css.input} value={form.symbol}
                  onChange={F("symbol")} placeholder="BTC/USDT, TONUSDT..." />
              </div>

              <div style={{ display:"grid", gridTemplateColumns:"1fr 1fr", gap:"10px" }}>
                <div style={css.field}>
                  <label style={css.label}>Tipo de señal</label>
                  <select style={css.select} value={form.signal_type} onChange={F("signal_type")}>
                    {SIGNAL_TYPES.map(s => <option key={s}>{s}</option>)}
                  </select>
                </div>
                <div style={css.field}>
                  <label style={css.label}>Tendencia</label>
                  <select style={css.select} value={form.trend} onChange={F("trend")}>
                    {TRENDS.map(t => <option key={t}>{t}</option>)}
                  </select>
                </div>
                <div style={css.field}>
                  <label style={css.label}>Nivel de riesgo</label>
                  <select style={css.select} value={form.risk_level} onChange={F("risk_level")}>
                    {RISK_LEVELS.map(r => <option key={r}>{r}</option>)}
                  </select>
                </div>
                <div style={css.field}>
                  <label style={css.label}>Tipo de entrada</label>
                  <select style={css.select} value={form.entry_type} onChange={F("entry_type")}>
                    {ENTRY_TYPES.map(e => <option key={e}>{e}</option>)}
                  </select>
                </div>
              </div>

              <div style={css.field}>
                <label style={css.label}>Contexto EMAs (21/55/144/233)</label>
                <input style={css.input} value={form.ema_context}
                  onChange={F("ema_context")} placeholder="Ej: Precio tocó EMA55, 55 sobre 144..." />
              </div>

              <div style={css.field}>
                <label style={css.label}>Contexto Bollinger Bands</label>
                <input style={css.input} value={form.bb_context}
                  onChange={F("bb_context")} placeholder="Ej: Squeeze activo, precio en banda lower..." />
              </div>

              <div style={{ display:"grid", gridTemplateColumns:"1fr 1fr 1fr", gap:"10px" }}>
                <div style={css.field}>
                  <label style={css.label}>Zona Entrada</label>
                  <input style={css.input} value={form.entry_zone}
                    onChange={F("entry_zone")} placeholder="precio" />
                </div>
                <div style={css.field}>
                  <label style={css.label}>Zona SL</label>
                  <input style={css.input} value={form.sl_zone}
                    onChange={F("sl_zone")} placeholder="precio" />
                </div>
                <div style={css.field}>
                  <label style={css.label}>Zona TP</label>
                  <input style={css.input} value={form.tp_zone}
                    onChange={F("tp_zone")} placeholder="precio" />
                </div>
              </div>

              <div style={css.field}>
                <label style={css.label}>Notas personales</label>
                <textarea
                  style={{ ...css.input, minHeight:"72px" }}
                  value={form.notes}
                  onChange={F("notes")}
                  placeholder="¿Qué hace especial este setup? Patrones de velas, confluencias..."
                />
              </div>

              {/* Preview de tags */}
              {form.signal_type && (
                <div style={{ marginBottom:"14px" }}>
                  <span style={css.tag(signalColor(form.signal_type))}>{form.signal_type}</span>
                  <span style={css.tag(form.trend === "BULLISH" ? COLORS.green : form.trend === "BEARISH" ? COLORS.red : COLORS.yellow)}>
                    {form.trend}
                  </span>
                  <span style={css.tag(COLORS.accent)}>{form.entry_type}</span>
                  <span style={css.tag(form.risk_level === "BAJO" ? COLORS.green : form.risk_level === "MEDIO" ? COLORS.yellow : COLORS.red)}>
                    RIESGO {form.risk_level}
                  </span>
                </div>
              )}

              <div style={{ display:"flex", gap:"10px" }}>
                <button
                  onClick={handleSave}
                  disabled={!form.symbol}
                  style={{
                    ...css.btn("success"), flex:1,
                    opacity: form.symbol ? 1 : 0.4,
                  }}
                >
                  ✓ Guardar Patrón
                </button>
                <button onClick={handleDiscard} style={css.btn("danger")}>
                  ✕ Descartar
                </button>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* TAB: BASE DE DATOS */}
      {activeTab === "db" && (
        <div style={css.card}>
          <div style={{ display:"flex", justifyContent:"space-between", alignItems:"center", marginBottom:"16px" }}>
            <div style={css.cardTitle}>Patrones guardados</div>
            <div style={{ display:"flex", gap:"8px" }}>
              {patterns.length > 0 && (
                <button onClick={handleExport} style={css.btn("primary")}>
                  ↓ Exportar JSON
                </button>
              )}
            </div>
          </div>

          {patterns.length === 0 ? (
            <div style={{
              textAlign:"center", padding:"48px 20px",
              color: COLORS.textDim, fontSize:"13px",
            }}>
              <div style={{ fontSize:"32px", marginBottom:"12px" }}>🗄</div>
              No hay patrones guardados aún.<br/>
              <span style={{ color: COLORS.muted }}>
                Sube un screenshot y analízalo para empezar.
              </span>
            </div>
          ) : (
            <>
              {/* Stats rápidas */}
              <div style={{
                display:"grid", gridTemplateColumns:"repeat(4,1fr)",
                gap:"12px", marginBottom:"20px",
              }}>
                {[
                  { label:"Total",    value: patterns.length,                                   color: COLORS.accent },
                  { label:"Longs",    value: patterns.filter(p=>p.signal_type?.includes("LONG")).length,  color: COLORS.green },
                  { label:"Shorts",   value: patterns.filter(p=>p.signal_type?.includes("SHORT")).length, color: COLORS.red },
                  { label:"Bajo riesgo", value: patterns.filter(p=>p.risk_level==="BAJO").length,        color: COLORS.yellow },
                ].map(s => (
                  <div key={s.label} style={{
                    background: COLORS.bg, borderRadius:"6px",
                    padding:"12px", textAlign:"center",
                    border:`1px solid ${s.color}33`,
                  }}>
                    <div style={{ fontSize:"22px", fontWeight:700, color:s.color }}>{s.value}</div>
                    <div style={{ fontSize:"10px", color:COLORS.textDim, letterSpacing:"0.06em" }}>{s.label.toUpperCase()}</div>
                  </div>
                ))}
              </div>

              {/* Lista de patrones */}
              <div style={{ border:`1px solid ${COLORS.border}`, borderRadius:"6px", overflow:"hidden" }}>
                <div style={{
                  ...css.dbRow,
                  background: COLORS.border + "66",
                  fontWeight:700, fontSize:"10px",
                  letterSpacing:"0.08em", color: COLORS.textDim,
                }}>
                  <span style={{width:"120px"}}>SÍMBOLO</span>
                  <span style={{width:"160px"}}>SEÑAL</span>
                  <span style={{width:"100px"}}>ENTRADA</span>
                  <span style={{flex:1}}>NOTAS</span>
                  <span style={{width:"80px"}}>FECHA</span>
                </div>
                {patterns.map(p => (
                  <div key={p.id} style={{
                    ...css.dbRow,
                    background: "transparent",
                    transition:"background 0.15s",
                  }}
                    onMouseEnter={e => e.currentTarget.style.background = COLORS.border+"44"}
                    onMouseLeave={e => e.currentTarget.style.background = "transparent"}
                  >
                    <span style={{ width:"120px", color: COLORS.accent, fontWeight:700 }}>
                      {p.symbol || "—"}
                    </span>
                    <span style={{ width:"160px" }}>
                      <span style={css.tag(signalColor(p.signal_type))}>
                        {p.signal_type}
                      </span>
                    </span>
                    <span style={{ width:"100px", color: COLORS.textDim }}>
                      {p.entry_type?.replace("_"," ")}
                    </span>
                    <span style={{ flex:1, color: COLORS.textDim, fontSize:"11px",
                      overflow:"hidden", textOverflow:"ellipsis", whiteSpace:"nowrap",
                      maxWidth:"200px"
                    }}>
                      {p.notes || p.ema_context || "—"}
                    </span>
                    <span style={{ width:"80px", color: COLORS.muted, fontSize:"10px" }}>
                      {new Date(p.timestamp).toLocaleDateString()}
                    </span>
                  </div>
                ))}
              </div>
            </>
          )}
        </div>
      )}

      {/* Toast */}
      {toast && (
        <div style={css.toast(toast.type)}>{toast.msg}</div>
      )}
    </div>
  );
} :"0.2519"},{"s":"LUNAUSDT","c":"0.0541","h":"0.05478","l":"0.0508","v":"1871753.6","qv":"100015.82","m":"0.0650","b1":"0.05416","a1":"0.05423"},{"s":"BMTUSDT","c":"0.01355","h":"0.01373","l":"0.01266","v":"820987.35","qv":"10920.72","m":"0.0695","b1":"0.01348","a1":"0.01349"},{"s":"NEIROCTOUSDT","c":"0.00007194","h":"0.00007387","l":"0.0000675","v":"1099506590","qv":"78501.89","m":"0.0644","b1":"0.00007203","a1":"0.00007209"},{"s":"CHZUSDC","c":"0.0258","h":"0.0258","l":"0.02483","v":"75351.04","qv":"1908.23","m":"0.0391","b1":"0.02579","a1":"0.02585"},{"s":"COINXUSDT","c":"170.98","h":"174.27","l":"160.64","v":"7149.524","qv":"1218400.05","m