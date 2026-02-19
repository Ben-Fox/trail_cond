const svgThumbUp = '<svg class="icon icon-xs" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 9V5a3 3 0 0 0-3-3l-4 9v11h11.28a2 2 0 0 0 2-1.7l1.38-9a2 2 0 0 0-2-2.3H14z"/><path d="M7 22H4a2 2 0 0 1-2-2v-7a2 2 0 0 1 2-2h3"/></svg>';
const svgThumbDown = '<svg class="icon icon-xs" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10 15v4a3 3 0 0 0 3 3l4-9V2H5.72a2 2 0 0 0-2 1.7l-1.38 9a2 2 0 0 0 2 2.3H10z"/><path d="M17 2h2.67A2.31 2.31 0 0 1 22 4v7a2.31 2.31 0 0 1-2.33 2H17"/></svg>';
const svgAccessible = '<svg class="icon icon-xs" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="4" r="2"/><path d="M9 22l3-9 3 9"/><path d="M7 12h10"/></svg>';

async function loadReports(facilityId) {
    const container = document.getElementById('reports-list');
    try {
        const res = await fetch(`/api/facility/${facilityId}/reports`);
        const data = await res.json();
        const reports = data.reports || [];
        if (!reports.length) {
            container.innerHTML = '<p class="no-reports">No reports yet. Be the first!</p>';
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
                        <span class="report-date">${r.date_visited || new Date(r.created_at).toLocaleDateString()}</span>
                    </div>
                    ${r.general_notes ? `<p class="report-notes">${escHtml(r.general_notes)}</p>` : ''}
                    ${r.accessibility_notes ? `<p class="report-notes"><em>${svgAccessible} ${escHtml(r.accessibility_notes)}</em></p>` : ''}
                    ${r.photo_url ? `<img class="report-photo" src="${escHtml(r.photo_url)}" alt="Report photo">` : ''}
                    <div class="report-footer">
                        <button class="vote-btn" onclick="vote(${r.id},'up',this)">${svgThumbUp} ${r.upvotes}</button>
                        <button class="vote-btn" onclick="vote(${r.id},'down',this)">${svgThumbDown} ${r.downvotes}</button>
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
            alert('Report submitted! Thank you.');
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
