import { useEffect, useState } from 'react'

const API_URL = import.meta.env.VITE_API_URL || 'http://localhost:8010'

const STATUS_FILTERS = ['all', 'failed', 'delivered', 'received']

function StatusBadge({ status }) {
  return <span className={`badge badge-${status}`}>{status}</span>
}

export default function App() {
  const [leads, setLeads] = useState([])
  const [filter, setFilter] = useState('failed')
  const [loading, setLoading] = useState(true)
  const [replayingId, setReplayingId] = useState(null)
  const [error, setError] = useState(null)
  const [crm, setCrm] = useState({ mock_mode: true, recovered: false })
  const [form, setForm] = useState({ name: '', email: '', website: '' })
  const [submitting, setSubmitting] = useState(false)
  const [notice, setNotice] = useState(null)

  async function fetchLeads() {
    setLoading(true)
    setError(null)
    try {
      const qs = filter === 'all' ? '' : `?status=${filter}`
      const res = await fetch(`${API_URL}/leads${qs}`)
      if (!res.ok) throw new Error(`Request failed (${res.status})`)
      setLeads(await res.json())
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }

  async function fetchCrmState() {
    try {
      const res = await fetch(`${API_URL}/mock-crm`)
      if (res.ok) setCrm(await res.json())
    } catch {
      /* banner is cosmetic; ignore */
    }
  }

  useEffect(() => {
    fetchLeads()
  }, [filter])

  useEffect(() => {
    fetchCrmState()
  }, [])

  async function toggleCrm() {
    const res = await fetch(`${API_URL}/mock-crm/toggle`, { method: 'POST' })
    setCrm(await res.json())
  }

  async function replay(id) {
    setReplayingId(id)
    setNotice(null)
    try {
      const res = await fetch(`${API_URL}/leads/${id}/replay`, { method: 'POST' })
      if (!res.ok) throw new Error(`Replay failed (${res.status})`)
      const lead = await res.json()
      setNotice(
        lead.status === 'delivered'
          ? `Replayed ${lead.name} — delivered to CRM.`
          : `Replayed ${lead.name} — still failing, left in the dead-letter queue.`,
      )
      await fetchLeads()
    } catch (err) {
      setError(err.message)
    } finally {
      setReplayingId(null)
    }
  }

  async function submitLead(e) {
    e.preventDefault()
    setSubmitting(true)
    setError(null)
    setNotice(null)
    try {
      const res = await fetch(`${API_URL}/leads`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ...form, source: 'dashboard_demo' }),
      })
      if (!res.ok) throw new Error(`Submission failed (${res.status})`)
      setNotice(`Lead "${form.name}" received — delivering to CRM in the background.`)
      setForm({ name: '', email: '', website: '' })
      setTimeout(fetchLeads, 1500)
    } catch (err) {
      setError(err.message)
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="page">
      <header className="header">
        <h1>LeadBridge</h1>
        <p className="subtitle">A lead lost between form and CRM is lost revenue no one notices.</p>
      </header>

      {crm.mock_mode && (
        <div className={`crm-banner ${crm.recovered ? 'up' : 'down'}`}>
          <span>
            Mock CRM: <strong>{crm.recovered ? 'healthy' : 'simulating outage'}</strong>
            {!crm.recovered && ' — @fail.dev leads will dead-letter'}
          </span>
          <button className="toggle-btn" onClick={toggleCrm}>
            {crm.recovered ? 'Simulate outage' : 'Simulate CRM recovery'}
          </button>
        </div>
      )}

      <form className="lead-form" onSubmit={submitLead}>
        <input
          required
          placeholder="Name"
          value={form.name}
          onChange={(e) => setForm({ ...form, name: e.target.value })}
        />
        <input
          required
          type="email"
          placeholder="Email (use @fail.dev to force a failure)"
          value={form.email}
          onChange={(e) => setForm({ ...form, email: e.target.value })}
        />
        {/* honeypot: hidden from humans, bots fill it and get silently dropped */}
        <input
          className="honeypot"
          tabIndex={-1}
          autoComplete="off"
          value={form.website}
          onChange={(e) => setForm({ ...form, website: e.target.value })}
        />
        <button type="submit" disabled={submitting}>
          {submitting ? 'Submitting…' : 'Submit test lead'}
        </button>
      </form>

      <div className="filters">
        {STATUS_FILTERS.map((s) => (
          <button
            key={s}
            className={`filter-btn ${filter === s ? 'active' : ''}`}
            onClick={() => setFilter(s)}
          >
            {s}
          </button>
        ))}
        <button className="refresh-btn" onClick={fetchLeads} disabled={loading}>
          {loading ? 'Refreshing…' : 'Refresh'}
        </button>
      </div>

      {error && <div className="error-banner">{error}</div>}
      {notice && <div className="notice-banner">{notice}</div>}

      <table className="leads-table">
        <thead>
          <tr>
            <th>Name</th>
            <th>Email</th>
            <th>Source</th>
            <th>Status</th>
            <th>Attempts</th>
            <th>Last error</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {leads.map((lead) => (
            <tr key={lead.id}>
              <td>{lead.name}</td>
              <td>{lead.email}</td>
              <td>{lead.source}</td>
              <td><StatusBadge status={lead.status} /></td>
              <td>{lead.attempts}</td>
              <td className="error-cell">{lead.last_error || '—'}</td>
              <td>
                {lead.status === 'failed' && (
                  <button
                    className="replay-btn"
                    onClick={() => replay(lead.id)}
                    disabled={replayingId === lead.id}
                  >
                    {replayingId === lead.id ? 'Replaying…' : 'Replay'}
                  </button>
                )}
              </td>
            </tr>
          ))}
          {!loading && leads.length === 0 && (
            <tr>
              <td colSpan={7} className="empty-state">No leads in this view.</td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  )
}
