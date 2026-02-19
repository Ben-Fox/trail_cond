async function loadReports(facilityId) {
    const container = document.getElementById('reports-list');
    try {
        const res = await fetch(`/api/facility/${facilityId}/reports`);
        const data = await res.json();
        const reports = data.reports || [];
        if (!reports.length) {
            container.innerHTML = '<p style="color:#636e72;">No reports yet. Be the first!</p>';
            return;
        }
        container.innerHTML = reports.map(r => {
            const sc = {'open':'good','partially_open':'caution','closed':'bad'}[r.status]||'unknown';
            return `
                <div class="report-item">
                    <div class="report-header">
                        <div class="report-badges">
                            <span class="badge badge-${sc}">${r.status.replace('_',' ')}</span>
                            ${r.trail_condition ? `<span class="badge badge-unknown">${r.trail_condition}</span>` : ''}
                            ${r.trash_level ? `<span class="badge badge-unknown">${r.trash_level.replace('_',' ')}</span>` : ''}
                        </div>
                        <span style="font-size:0.8rem;color:#636e72;">${r.date_visited || new Date(r.created_at).toLocaleDateString()}</span>
                    </div>
                    ${r.general_notes ? `<p class="report-notes">${escHtml(r.general_notes)}</p>` : ''}
                    ${r.accessibility_notes ? `<p class="report-notes"><em>♿ ${escHtml(r.accessibility_notes)}</em></p>` : ''}
                    ${r.photo_url ? `<img class="report-photo" src="${escHtml(r.photo_url)}" alt="Report photo">` : ''}
                    <div class="report-footer">
                        <button class="vote-btn" onclick="vote(${r.id},'up',this)">👍 ${r.upvotes}</button>
                        <button class="vote-btn" onclick="vote(${r.id},'down',this)">👎 ${r.downvotes}</button>
                    </div>
                </div>
            `;
        }).join('');
    } catch(e) {
        container.innerHTML = `<p class="error">Failed to load reports</p>`;
    }
}

async function submitReport(e) {
    e.preventDefault();
    const form = e.target;
    const data = Object.fromEntries(new FormData(form));
    data.facility_name = document.querySelector('.facility-header h1')?.textContent || '';
    
    try {
        const res = await fetch(`/api/facility/${facilityId}/reports`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(data)
        });
        if (res.ok) {
            form.reset();
            loadReports(facilityId);
            alert('Report submitted! Thank you 🌲');
        } else {
            alert('Failed to submit report');
        }
    } catch(e) {
        alert('Error: ' + e.message);
    }
}

async function vote(reportId, type, btn) {
    try {
        const res = await fetch(`/api/reports/${reportId}/vote`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({vote: type})
        });
        if (res.ok) {
            loadReports(facilityId);
        }
    } catch(e) {}
}

function escHtml(s) {
    const d = document.createElement('div');
    d.textContent = s;
    return d.innerHTML;
}
