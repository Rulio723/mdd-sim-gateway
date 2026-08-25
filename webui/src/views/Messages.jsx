import React, { useEffect, useState, useCallback, useRef } from 'react'
import { api } from '../api.js'
import SimSelector from './SimSelector.jsx'
import { useI18n } from '../i18n.jsx'

export default function Messages({ selected, subscribe, showToast, instances, cards, devices, setSelected }) {
  const { t: tr } = useI18n()
  const id = selected?.id
  const [threads, setThreads] = useState([])
  const [threadsLoading, setThreadsLoading] = useState(false)
  const [peer, setPeer] = useState(null)
  const [msgs, setMsgs] = useState([])
  const [messagesLoading, setMessagesLoading] = useState(false)
  const [text, setText] = useState('')
  const [newTo, setNewTo] = useState('')
  const [transport, setTransport] = useState('auto')
  const [sending, setSending] = useState(false)
  const [selMode, setSelMode] = useState(false)      // multi-select messages to delete
  const [selIds, setSelIds] = useState(() => new Set())
  const [binary, setBinary] = useState([])           // filed non-text payloads (see BinaryPayloads)
  const activeId = useRef(id)
  const activePeer = useRef(peer)
  const threadsRequest = useRef(0)
  const messagesRequest = useRef(0)
  const sendingRef = useRef(false)
  activeId.current = id
  activePeer.current = peer

  // Cellular SMS is available only when this line is currently attached to a live modem.
  // Older backends do not expose a dedicated SMS capability, so use the unified device type
  // instead; the backend still performs the authoritative ModemManager capability check.
  const selectedDevice = devices.find((device) => device.present === true
    && device.device_type === 'modem'
    && String(device.instance_id || '') === String(id || ''))
  const cellularAvailable = Boolean(selectedDevice)

  const loadThreads = useCallback(async (showLoading = false) => {
    if (!id) return
    const request = ++threadsRequest.current
    if (showLoading) setThreadsLoading(true)
    try {
      const r = await api.threads(id)
      if (request === threadsRequest.current && activeId.current === id) setThreads(r.threads)
    } catch {}
    finally {
      if (request === threadsRequest.current && activeId.current === id) setThreadsLoading(false)
    }
  }, [id])

  // Filed payloads never arrive through the SMS websocket event (they are deliberately not
  // broadcast), so they are fetched alongside the threads rather than pushed. A backend that
  // predates the endpoint simply yields an empty list and the panel stays hidden.
  const loadBinary = useCallback(async () => {
    if (!id) return
    try {
      const r = await api.binarySms(id)
      if (activeId.current === id) setBinary(r.payloads || [])
    } catch { if (activeId.current === id) setBinary([]) }
  }, [id])

  const loadMsgs = useCallback(async (p, showLoading = false) => {
    if (!id || !p) return
    const request = ++messagesRequest.current
    if (showLoading) setMessagesLoading(true)
    try {
      const r = await api.messages(id, p)
      if (request === messagesRequest.current && activeId.current === id && activePeer.current === p) setMsgs(r.messages)
    } catch {}
    finally {
      if (request === messagesRequest.current && activeId.current === id && activePeer.current === p) setMessagesLoading(false)
    }
  }, [id])

  // A conversation key is only meaningful inside one line. Clear the old line's local view
  // synchronously when switching SIMs; loadThreads then fills the selected line's history.
  // Without this, an old peer can trigger an empty lookup on the new line and make its existing
  // history appear to have disappeared.
  useEffect(() => {
    ++threadsRequest.current; ++messagesRequest.current
    setThreads([]); setPeer(null); setMsgs([]); setText(''); setNewTo(''); setTransport('auto')
    setBinary([])
    setThreadsLoading(Boolean(id)); setMessagesLoading(false)
    if (id) { loadThreads(true); loadBinary() }
  }, [id, loadThreads, loadBinary])
  useEffect(() => {
    if (!cellularAvailable && transport === 'cellular') setTransport('auto')
  }, [cellularAvailable, transport])
  useEffect(() => {
    ++messagesRequest.current
    setMsgs([])
    setMessagesLoading(Boolean(peer))
    if (peer) loadMsgs(peer, true)
  }, [peer, loadMsgs])
  // leaving/refreshing a thread resets the selection UI
  useEffect(() => { setSelMode(false); setSelIds(new Set()) }, [peer])
  // if the open conversation empties (delete/clear), leave select mode so its toolbar
  // (rendered only while msgs.length>0) can't strand the UI in select state.
  useEffect(() => { if (!msgs.length) { setSelMode(false); setSelIds(new Set()) } }, [msgs.length])
  useEffect(() => subscribe((msg) => {
    if (msg.type === 'sms' && msg.instance === id) {
      loadThreads()
      loadBinary()
      if (peer) loadMsgs(peer)
    }
  }), [subscribe, id, peer, loadThreads, loadMsgs, loadBinary])

  const send = async () => {
    // React state is updated asynchronously, so `sending` alone leaves a short window where
    // a double click or a repeating Enter key can submit the same billable SMS twice.
    if (sendingRef.current) return
    const to = peer || newTo
    if (!to || !text) return
    const forId = id
    sendingRef.current = true
    setSending(true)
    try {
      const res = await api.sendSms(forId, to, text, transport)
      // A slow modem submit may finish after the operator selected another line. Never erase
      // that line's draft or replace its open conversation with the old line's recipient.
      if (activeId.current === forId) {
        setText(''); setPeer(to); setNewTo('')
        await loadThreads(); await loadMsgs(to)
      }
      if (res && res.ok === false) {
        const msg = res.uncertain
          ? tr('SMS submission timed out; delivery is unknown. Do not retry automatically.')
          : 'SMS not delivered: ' + (res.error || 'unknown error')
        showToast ? showToast(msg) : alert(msg)
      }
    } catch (e) {
      const msg = 'SMS failed: ' + e.message
      showToast ? showToast(msg) : alert(msg)
    } finally {
      sendingRef.current = false
      setSending(false)
    }
  }

  const toast = (m) => (showToast ? showToast(m) : null)

  const toggleSel = (mid) => setSelIds((s) => {
    const n = new Set(s); n.has(mid) ? n.delete(mid) : n.add(mid); return n
  })
  // The awaited delete may resolve after the user switched SIM lines — only refresh if
  // we're still on the same line, so we don't write the old line's data into state.
  const refreshIfSame = async (forId, p) => {
    if (forId !== id) return
    await loadThreads(); if (p) await loadMsgs(p)
  }

  const deleteSelected = async () => {
    if (!selIds.size) return
    if (!confirm(`Delete ${selIds.size} selected message${selIds.size > 1 ? 's' : ''}?`)) return
    const forId = id, p = peer
    try {
      await api.deleteMessages(forId, { ids: [...selIds] })
      setSelMode(false); setSelIds(new Set())
      await refreshIfSame(forId, p)
      toast('Messages deleted')
    } catch (e) { toast('Delete failed: ' + e.message) }
  }

  const deleteThread = async (p, e) => {
    if (e) e.stopPropagation()
    if (!confirm(`Delete the entire conversation with ${p}? This removes all its messages.`)) return
    const forId = id
    try {
      await api.deleteMessages(forId, { peer: p })
      if (peer === p) { setPeer(null); setMsgs([]) }
      if (forId === id) await loadThreads()
      toast('Conversation deleted')
    } catch (e2) { toast('Delete failed: ' + e2.message) }
  }

  const clearAll = async () => {
    if (!threads.length) return
    if (!confirm('Delete ALL messages on this line? This cannot be undone.')) return
    const forId = id
    try {
      await api.deleteMessages(forId, { all: true })
      if (forId === id) { setPeer(null); setMsgs([]); await loadThreads() }
      toast('All messages deleted')
    } catch (e) { toast('Delete failed: ' + e.message) }
  }

  if (!id) return (
    <div>
      <SimSelector instances={instances} cards={cards} devices={devices} selected={selected} setSelected={setSelected} />
      <div style={{ color: 'var(--text-dim)' }}>{tr('Select a SIM / line to view and send messages.')}</div>
    </div>
  )

  return (
    <div style={{ height: '100%', display: 'flex', flexDirection: 'column' }}>
      <div style={{ flexShrink: 0 }}>
        <SimSelector instances={instances} cards={cards} devices={devices} selected={selected} setSelected={setSelected} />
      </div>
      <div style={{ display: 'grid', gridTemplateColumns: '280px 1fr', gridTemplateRows: 'minmax(0, 1fr)', gap: 16, flex: 1, minHeight: 0 }}>
      <div className="card" style={{ padding: 12, overflow: 'auto', minHeight: 0 }}>
        <button className="btn btn-primary" style={{ width: '100%', marginBottom: 8 }} onClick={() => { setPeer(null); setMsgs([]); setMessagesLoading(false) }}>+ {tr('New message')}</button>
        {threads.length > 0 &&
          <button className="btn btn-ghost" style={{ width: '100%', marginBottom: 10, color: '#ef4444', fontSize: 12 }}
            onClick={clearAll}>{tr('Clear all conversations')}</button>}
        {threads.map((t) => (
          <div key={t.peer} onClick={() => setPeer(t.peer)} className="hover-row"
            style={{ padding: 10, borderRadius: 10, cursor: 'pointer', marginBottom: 4, display: 'flex', alignItems: 'center', gap: 8,
              background: peer === t.peer ? 'var(--active)' : 'transparent' }}>
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ fontWeight: 600, fontSize: 14 }} className="mono">{t.peer}</div>
              <div style={{ fontSize: 12, color: 'var(--text-mute)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{t.last_body}</div>
            </div>
            <button className="row-del" title="Delete conversation" aria-label={`Delete conversation with ${t.peer}`}
              onClick={(e) => deleteThread(t.peer, e)}>🗑</button>
          </div>
        ))}
        {threadsLoading && <div aria-live="polite" style={{ color: 'var(--text-mute)', fontSize: 13, padding: 8 }}>{tr('Loading conversations…')}</div>}
        {!threadsLoading && threads.length === 0 && <div style={{ color: 'var(--text-mute)', fontSize: 13, padding: 8 }}>{tr('No conversations yet.')}</div>}
        <BinaryPayloads payloads={binary} tr={tr} />
      </div>

      <div className="card" style={{ display: 'flex', flexDirection: 'column', padding: 0, minHeight: 0 }}>
        <div style={{ padding: 14, borderBottom: '1px solid var(--border)', display: 'flex', alignItems: 'center', gap: 10, flexShrink: 0 }}>
          {peer ? <span className="mono" style={{ fontWeight: 600, flex: 1 }}>{peer}</span>
            : <input placeholder={tr('Recipient number e.g. +1...')} value={newTo} onChange={(e) => setNewTo(e.target.value)} style={{ maxWidth: 300, flex: 1 }} />}
          {peer && msgs.length > 0 && (
            selMode ? (
              <>
                <span style={{ fontSize: 12, color: 'var(--text-mute)' }}>{selIds.size} {tr('selected')}</span>
                <button className="btn btn-ghost" style={{ padding: '4px 10px', fontSize: 12, color: '#ef4444' }}
                  disabled={!selIds.size} onClick={deleteSelected}>{tr('Delete')}</button>
                <button className="btn btn-ghost" style={{ padding: '4px 10px', fontSize: 12 }}
                  onClick={() => { setSelMode(false); setSelIds(new Set()) }}>{tr('Cancel')}</button>
              </>
            ) : (
              <>
                <button className="btn btn-ghost" style={{ padding: '4px 10px', fontSize: 12 }}
                  onClick={() => setSelMode(true)}>{tr('Select')}</button>
                <button className="btn btn-ghost" title="Delete conversation" style={{ padding: '4px 10px', fontSize: 12, color: '#ef4444' }}
                  onClick={() => deleteThread(peer)}>{tr('Delete all')}</button>
              </>
            )
          )}
        </div>
        <div style={{ flex: 1, minHeight: 0, overflow: 'auto', padding: 16, display: 'flex', flexDirection: 'column', gap: 8 }}>
          {messagesLoading && <div aria-live="polite" style={{ color: 'var(--text-mute)', fontSize: 13 }}>{tr('Loading messages…')}</div>}
          {!messagesLoading && peer && msgs.length === 0 && <div style={{ color: 'var(--text-mute)', fontSize: 13 }}>{tr('No messages in this conversation.')}</div>}
          {msgs.map((m) => {
            const failed = m.status === 'failed'
            // Outbound delivery lifecycle: pending -> sent (IMS accepted) -> delivered | failed.
            // 'delivered' is confirmed by the network's SMS submit report; 'sent' means accepted
            // but delivery not yet confirmed.
            const delivered = m.status === 'delivered'
            const sent = m.status === 'sent'
            const uncertain = m.status === 'unknown'
            const statusText = failed ? ` · ${tr('Failed to deliver')}`
              : m.status === 'pending' ? ` · ${tr('sending…')}`
              : sent ? ` · ${tr('Sent')}`
              : delivered ? ` · ${tr('Delivered ✓')}`
              : uncertain ? ` · ${tr('Delivery unknown')}`
              : ''
            const statusColor = failed ? '#ef4444' : uncertain ? '#f59e0b' : delivered ? '#22c55e' : 'var(--text-mute)'
            const checked = selIds.has(m.id)
            return (
              <div key={m.id} onClick={() => selMode && toggleSel(m.id)}
                style={{ alignSelf: m.direction === 'out' ? 'flex-end' : 'flex-start', maxWidth: '74%',
                  cursor: selMode ? 'pointer' : 'default', display: 'flex', alignItems: 'center', gap: 8,
                  flexDirection: m.direction === 'out' ? 'row-reverse' : 'row' }}>
                {selMode && <input type="checkbox" readOnly checked={checked} style={{ width: 'auto', flexShrink: 0 }} />}
                <div style={{ minWidth: 0 }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 6,
                    flexDirection: m.direction === 'out' ? 'row' : 'row-reverse' }}>
                    {failed && <span title={m.error || 'Delivery failed'}
                      style={{ color: '#ef4444', fontWeight: 800, cursor: 'help', fontSize: 15 }}>❗</span>}
                    {uncertain && <span title={m.error || tr('Delivery unknown')}
                      style={{ color: '#f59e0b', fontWeight: 800, cursor: 'help', fontSize: 15 }}>⚠</span>}
                    <div style={{
                      background: checked ? 'var(--active)' : failed ? 'rgba(239,68,68,.15)' : uncertain ? 'rgba(245,158,11,.14)' : (m.direction === 'out' ? 'var(--primary)' : 'var(--hover)'),
                      border: failed ? '1px solid rgba(239,68,68,.55)' : uncertain ? '1px solid rgba(245,158,11,.55)' : '1px solid transparent',
                      padding: '8px 12px', borderRadius: 12, fontSize: 14,
                    }}>{m.body}</div>
                  </div>
                  <div style={{ fontSize: 10, color: statusColor,
                    textAlign: m.direction === 'out' ? 'right' : 'left', marginTop: 2 }}>
                    {new Date(m.ts * 1000).toLocaleString()}
                    {m.transport === 'cellular' ? ` · ${tr('4G SMS')}` : ''}
                    {statusText}
                  </div>
                  {failed && m.error && (
                    <div style={{ fontSize: 10.5, color: '#ef4444', marginTop: 1,
                      textAlign: m.direction === 'out' ? 'right' : 'left', maxWidth: 280 }}>{m.error}</div>
                  )}
                  {uncertain && m.error && (
                    <div style={{ fontSize: 10.5, color: '#f59e0b', marginTop: 1,
                      textAlign: m.direction === 'out' ? 'right' : 'left', maxWidth: 280 }}>{m.error}</div>
                  )}
                </div>
              </div>
            )
          })}
        </div>
        <div style={{ display: 'flex', gap: 8, padding: 12, borderTop: '1px solid var(--border)', flexShrink: 0, flexWrap: 'wrap', alignItems: 'center' }}>
          <label style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 12, color: 'var(--text-mute)', whiteSpace: 'nowrap' }}>
            {tr('Send via')}
            <select value={transport} disabled={sending}
              onChange={(e) => setTransport(e.target.value)}
              aria-label={tr('Send via')}
              title={!cellularAvailable ? tr('This line does not have an available cellular modem.') : ''}
              style={{ width: 'auto', minWidth: 150 }}>
              <option value="auto">{tr('Auto (VoWiFi first)')}</option>
              <option value="vowifi">VoWiFi</option>
              <option value="cellular" disabled={!cellularAvailable}>
                {tr('Cellular network (Modem)')}{!cellularAvailable ? ` — ${tr('Unavailable')}` : ''}
              </option>
            </select>
          </label>
          <input placeholder={tr('Type a message…')} value={text} disabled={sending}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => {
              if (e.key !== 'Enter') return
              e.preventDefault()
              if (!e.repeat) send()
            }} style={{ flex: '1 1 220px' }} />
          <button className="btn btn-primary" disabled={sending || (!peer && !newTo)} onClick={send}>{tr('Send')}</button>
        </div>
      </div>
      </div>
    </div>
  )
}

// Payloads that were filed instead of shown: binary SMS, SIM-addressed messages, silent
// service pushes. Deliberately reachable rather than invisible — the classification reads the
// PDU header, and a carrier that mislabels a real text's TP-DCS would otherwise hide it for
// good with no way to notice. Collapsed by default so it costs nothing when there is nothing
// to see, and never renders at all when the line has received none.
const PAYLOAD_TAG_LABEL = {
  '8bit': '8-bit binary data',
  sim_class: 'Addressed to the SIM (class 2)',
  sim_download: 'SIM data download',
  unreported: 'Classified by content — this engine does not report the PDU header',
}

function BinaryPayloads({ payloads, tr }) {
  const [open, setOpen] = useState(false)
  const [shown, setShown] = useState(() => new Set())
  if (!payloads.length) return null
  const toggle = (id) => setShown((prev) => {
    const next = new Set(prev)
    next.has(id) ? next.delete(id) : next.add(id)
    return next
  })
  return (
    <div style={{ marginTop: 12, borderTop: '1px solid var(--border)', paddingTop: 8 }}>
      <div onClick={() => setOpen(!open)} role="button" tabIndex={0}
        onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setOpen(!open) } }}
        style={{ cursor: 'pointer', fontSize: 12, color: 'var(--text-mute)', display: 'flex', alignItems: 'center', gap: 6, padding: '4px 6px', borderRadius: 8 }}>
        <span style={{ fontSize: 10 }}>{open ? '▾' : '▸'}</span>
        <span style={{ flex: 1 }}>{tr('Non-text payloads')}</span>
        <span className="mono">{payloads.length}</span>
      </div>
      {open && (
        <div style={{ marginTop: 4 }}>
          <div style={{ fontSize: 11, color: 'var(--text-mute)', padding: '2px 6px 8px', lineHeight: 1.5 }}>
            {tr('Messages addressed to the SIM or to an application rather than to you. They are kept out of your conversations but not discarded.')}
          </div>
          {payloads.map((p) => (
            <div key={p.id} style={{ padding: '6px 6px 7px', borderRadius: 8, marginBottom: 2, background: 'var(--hover)' }}>
              <div onClick={() => toggle(p.id)} style={{ cursor: 'pointer', display: 'flex', alignItems: 'baseline', gap: 6 }}>
                <span className="mono" style={{ fontSize: 12, fontWeight: 600 }}>{p.peer}</span>
                <span style={{ fontSize: 10, color: 'var(--text-mute)', flex: 1 }}>
                  {new Date(p.ts * 1000).toLocaleString()}
                </span>
                <span className="mono" style={{ fontSize: 10, color: 'var(--text-mute)' }}>
                  {tr('{n} bytes', { n: Math.floor((p.body_hex || '').length / 2) })}
                </span>
              </div>
              <div style={{ fontSize: 10, color: 'var(--text-mute)', marginTop: 2 }}>
                {(p.tags || []).map((tag) => tr(PAYLOAD_TAG_LABEL[tag] || tag)).join(' · ')}
                {p.concat_total ? ` · ${tr('part {seq}/{total}', { seq: p.concat_seq, total: p.concat_total })}` : ''}
              </div>
              {shown.has(p.id) && (
                <div style={{ marginTop: 6 }}>
                  {/* The bytes as they arrived. An encrypted payload can only be identified from
                      the PDU itself, so this is shown raw rather than decoded into anything. */}
                  <div style={{ fontSize: 10, color: 'var(--text-mute)', marginBottom: 2 }}>{tr('Payload')}</div>
                  <div className="mono" style={{ fontSize: 10, wordBreak: 'break-all', lineHeight: 1.5, color: 'var(--text-mute)' }}>
                    {p.body_hex || '—'}
                  </div>
                  {p.udh_hex && (
                    <>
                      <div style={{ fontSize: 10, color: 'var(--text-mute)', margin: '5px 0 2px' }}>{tr('User data header')}</div>
                      <div className="mono" style={{ fontSize: 10, wordBreak: 'break-all', color: 'var(--text-mute)' }}>{p.udh_hex}</div>
                    </>
                  )}
                  <div style={{ fontSize: 10, color: 'var(--text-mute)', marginTop: 5 }}>
                    {p.tp_dcs === null || p.tp_dcs === undefined
                      ? tr('TP-DCS not reported')
                      : `TP-DCS 0x${Number(p.tp_dcs).toString(16).padStart(2, '0')} · TP-PID 0x${Number(p.tp_pid || 0).toString(16).padStart(2, '0')}`}
                  </div>
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
