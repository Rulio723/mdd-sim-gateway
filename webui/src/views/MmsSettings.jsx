import React, { useEffect, useState } from 'react'
import { api } from '../api.js'
import { useI18n } from '../i18n.jsx'

// Simple fixed-position overlay modal, matching the pattern used by Esim.jsx's RenameModal /
// DownloadModal: a translucent backdrop that closes on click, a centered card that stops
// that click from bubbling.
export default function MmsSettings({ id, onClose, showToast }) {
  const { t } = useI18n()
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState(false)
  const [saving, setSaving] = useState(false)
  const [effective, setEffective] = useState(null)
  const [line, setLine] = useState(null)
  const [form, setForm] = useState(null)

  useEffect(() => {
    let alive = true
    setLoading(true); setLoadError(false)
    api.mmsSettings(id).then((r) => {
      if (!alive) return
      setEffective(r.effective)
      setLine(r.line)
      const own = r.line || {}
      setForm({
        enabled: own.enabled !== undefined ? Boolean(own.enabled) : Boolean(r.effective.enabled),
        auto_download: own.auto_download !== undefined ? Boolean(own.auto_download) : Boolean(r.effective.auto_download),
        transport: own.transport || 'auto',
        apn: own.apn || '',
        mmsc: own.mmsc || '',
        proxy: own.proxy || '',
        username: own.username || '',
        password: '',
        clear_password: false,
        user_agent: own.user_agent || '',
        max_size_kb: String(Math.max(1, Math.round((r.effective.max_size || 300 * 1024) / 1024))),
      })
    }).catch(() => { if (alive) setLoadError(true) })
      .finally(() => { if (alive) setLoading(false) })
    return () => { alive = false }
  }, [id])

  const setField = (key) => (e) => {
    const value = e.target.type === 'checkbox' ? e.target.checked : e.target.value
    setForm((f) => ({ ...f, [key]: value }))
  }

  const save = async () => {
    if (!form || saving) return
    setSaving(true)
    try {
      const body = {
        enabled: form.enabled,
        auto_download: form.auto_download,
        transport: form.transport,
        apn: form.apn,
        mmsc: form.mmsc,
        proxy: form.proxy,
        username: form.username,
        user_agent: form.user_agent,
        max_size: Math.max(30, Number(form.max_size_kb) || 300) * 1024,
      }
      if (form.clear_password) body.clear_password = true
      else if (form.password) body.password = form.password
      await api.saveMmsSettings(id, body)
      showToast ? showToast(t('MMS settings saved')) : null
      onClose()
    } catch (e) {
      const msg = t('Save failed') + ': ' + e.message
      showToast ? showToast(msg) : alert(msg)
    } finally {
      setSaving(false)
    }
  }

  return (
    <div style={{ position: 'fixed', inset: 0, background: '#0008', display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 60 }}
      onClick={onClose}>
      <div className="card" style={{ width: 460, maxWidth: '92vw', maxHeight: '86vh', overflow: 'auto', padding: 20 }}
        onClick={(e) => e.stopPropagation()}>
        <div style={{ fontWeight: 700, fontSize: 16, marginBottom: 12 }}>{t('MMS settings')}</div>
        {loading && <div style={{ color: 'var(--text-mute)', fontSize: 13 }}>{t('Loading')}…</div>}
        {!loading && loadError && <div className="u-error" style={{ fontSize: 13 }}>{t('Loading failed')}</div>}
        {!loading && !loadError && form && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
            <label style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 13 }}>
              <input type="checkbox" checked={form.enabled} onChange={setField('enabled')} style={{ width: 'auto' }} />
              {t('Enabled')}
            </label>
            <label style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 13 }}>
              <input type="checkbox" checked={form.auto_download} onChange={setField('auto_download')} style={{ width: 'auto' }} />
              {t('Auto-download')}
            </label>
            <label style={{ display: 'block' }}>
              <div style={{ fontSize: 12, color: 'var(--text-mute)', marginBottom: 4 }}>{t('Transport')}</div>
              <select value={form.transport} onChange={setField('transport')} style={{ width: '100%' }}>
                <option value="auto">{t('Automatic — modem if possible, else host')}</option>
                <option value="modem">{t('Modem (Quectel embedded TCP/IP)')}</option>
                <option value="host">{t('Host network')}</option>
              </select>
            </label>
            <label style={{ display: 'block' }}>
              <div style={{ fontSize: 12, color: 'var(--text-mute)', marginBottom: 4 }}>{t('APN')}</div>
              <input value={form.apn} onChange={setField('apn')} placeholder={effective?.apn || ''} style={{ width: '100%' }} />
            </label>
            <label style={{ display: 'block' }}>
              <div style={{ fontSize: 12, color: 'var(--text-mute)', marginBottom: 4 }}>{t('MMSC URL')}</div>
              <input value={form.mmsc} onChange={setField('mmsc')} placeholder={effective?.mmsc || 'http://...'} style={{ width: '100%' }} />
              <div style={{ fontSize: 11, color: 'var(--text-mute)', marginTop: 4 }}>
                {t('Leaving this empty uses the carrier settings detected for this SIM.')}
              </div>
            </label>
            <label style={{ display: 'block' }}>
              <div style={{ fontSize: 12, color: 'var(--text-mute)', marginBottom: 4 }}>{t('Proxy')}</div>
              <input value={form.proxy} onChange={setField('proxy')} placeholder={effective?.proxy || 'host:port'} style={{ width: '100%' }} />
            </label>
            <label style={{ display: 'block' }}>
              <div style={{ fontSize: 12, color: 'var(--text-mute)', marginBottom: 4 }}>{t('Username')}</div>
              <input value={form.username} onChange={setField('username')} placeholder={effective?.username || ''} style={{ width: '100%' }} />
            </label>
            <label style={{ display: 'block' }}>
              <div style={{ fontSize: 12, color: 'var(--text-mute)', marginBottom: 4 }}>{t('Password')}</div>
              <input type="password" value={form.password}
                onChange={(e) => setForm((f) => ({ ...f, password: e.target.value, clear_password: false }))}
                placeholder={line?.password_set ? t('unchanged') : ''} style={{ width: '100%' }} />
              {line?.password_set && (
                <label style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 11, color: 'var(--text-mute)', marginTop: 4 }}>
                  <input type="checkbox" checked={form.clear_password} style={{ width: 'auto' }}
                    onChange={(e) => setForm((f) => ({ ...f, clear_password: e.target.checked, password: '' }))} />
                  {t('Clear saved password')}
                </label>
              )}
            </label>
            <label style={{ display: 'block' }}>
              <div style={{ fontSize: 12, color: 'var(--text-mute)', marginBottom: 4 }}>{t('Size limit (KB)')}</div>
              <input type="number" min={30} value={form.max_size_kb} onChange={setField('max_size_kb')} style={{ width: '100%' }} />
            </label>
            <label style={{ display: 'block' }}>
              <div style={{ fontSize: 12, color: 'var(--text-mute)', marginBottom: 4 }}>{t('User-Agent')}</div>
              <input value={form.user_agent} onChange={setField('user_agent')} placeholder={effective?.user_agent || ''} style={{ width: '100%' }} />
            </label>
            <div style={{ fontSize: 11, color: 'var(--text-mute)', lineHeight: 1.5, borderTop: '1px solid var(--border)', paddingTop: 10 }}>
              {effective?.detected
                ? t('Detected: {name} · APN {apn} · MMSC {mmsc} · proxy {proxy}', {
                  name: effective.detected.name || t('Unknown carrier'),
                  apn: effective.detected.apn || '—',
                  mmsc: effective.detected.mmsc || '—',
                  proxy: effective.detected.proxy || '—',
                })
                : t('No carrier settings detected; enter the MMSC to enable MMS.')}
            </div>
            <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end', marginTop: 4 }}>
              <button className="btn btn-ghost" onClick={onClose} disabled={saving}>{t('Cancel')}</button>
              <button className="btn btn-primary" onClick={save} disabled={saving}>{t(saving ? 'Saving…' : 'Save')}</button>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
