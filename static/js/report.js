const STATES = ['AL','AK','AZ','AR','CA','CO','CT','DE','FL','GA','HI','ID','IL','IN','IA','KS','KY','LA','ME','MD','MA','MI','MN','MS','MO','MT','NE','NV','NH','NJ','NM','NY','NC','ND','OH','OK','OR','PA','RI','SC','SD','TN','TX','UT','VT','VA','WA','WV','WI','WY'];
const STATE_NAMES = {'AL':'Alabama','AK':'Alaska','AZ':'Arizona','AR':'Arkansas','CA':'California','CO':'Colorado','CT':'Connecticut','DE':'Delaware','FL':'Florida','GA':'Georgia','HI':'Hawaii','ID':'Idaho','IL':'Illinois','IN':'Indiana','IA':'Iowa','KS':'Kansas','KY':'Kentucky','LA':'Louisiana','ME':'Maine','MD':'Maryland','MA':'Massachusetts','MI':'Michigan','MN':'Minnesota','MS':'Mississippi','MO':'Missouri','MT':'Montana','NE':'Nebraska','NV':'Nevada','NH':'New Hampshire','NJ':'New Jersey','NM':'New Mexico','NY':'New York','NC':'North Carolina','ND':'North Dakota','OH':'Ohio','OK':'Oklahoma','OR':'Oregon','PA':'Pennsylvania','RI':'Rhode Island','SC':'South Carolina','SD':'South Dakota','TN':'Tennessee','TX':'Texas','UT':'Utah','VT':'Vermont','VA':'Virginia','WA':'Washington','WV':'West Virginia','WI':'Wisconsin','WY':'Wyoming'};

let searchTimeout;

document.addEventListener('DOMContentLoaded', () => {
    const sel = document.getElementById('state-select');
    STATES.forEach(s => { const o = document.createElement('option'); o.value = s; o.textContent = STATE_NAMES[s]||s; sel.appendChild(o); });

    const input = document.getElementById('fac-name-input');
    const list = document.getElementById('autocomplete-list');
    input.addEventListener('input', () => {
        clearTimeout(searchTimeout);
        const val = input.value.trim();
        if(val.length < 3) { list.classList.remove('show'); return; }
        searchTimeout = setTimeout(() => {
            fetch(`/api/search?query=${encodeURIComponent(val)}&limit=8`).then(r=>r.json()).then(data => {
                const results = data.results || [];
                if(!results.length) { list.classList.remove('show'); return; }
                list.innerHTML = results.map(r => `<div class="autocomplete-item" data-id="${r.id}" data-name="${r.name}">${r.name} <small style="color:var(--gray)">${r.type||''} ${r.state||''}</small></div>`).join('');
                list.classList.add('show');
                list.querySelectorAll('.autocomplete-item').forEach(item => {
                    item.addEventListener('click', () => {
                        input.value = item.dataset.name;
                        document.getElementById('fac-id-input').value = item.dataset.id;
                        list.classList.remove('show');
                    });
                });
            });
        }, 300);
    });
    document.addEventListener('click', e => { if(!e.target.closest('.autocomplete-wrapper')) list.classList.remove('show'); });
});

function setCleanliness(val) {
    document.getElementById('cleanliness-val').value = val;
    document.querySelectorAll('.cleanliness-btn').forEach(b => {
        b.classList.toggle('active', parseInt(b.dataset.val) <= val);
    });
}

document.getElementById('standalone-form').addEventListener('submit', e => {
    e.preventDefault();
    const fd = new FormData(e.target);
    const hazards = [];
    document.querySelectorAll('input[name="hazards"]:checked').forEach(c => hazards.push(c.value));
    const data = {
        facility_id: fd.get('facility_id') || '',
        facility_name: fd.get('facility_name') || '',
        state: fd.get('state') || '',
        area_region: fd.get('area_region') || '',
        date_visited: fd.get('date_visited') || '',
        overall_condition: fd.get('overall_condition') || '',
        cleanliness: parseInt(fd.get('cleanliness')) || 0,
        trail_surface: fd.get('trail_surface') || '',
        road_access: fd.get('road_access') || '',
        crowding: fd.get('crowding') || '',
        water_availability: fd.get('water_availability') || '',
        restroom_condition: fd.get('restroom_condition') || '',
        general_notes: fd.get('general_notes') || '',
        hazards: hazards.join(','),
        recommend: fd.get('recommend') || ''
    };
    fetch('/api/reports/standalone', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(data)})
    .then(r=>r.json()).then(() => {
        document.getElementById('standalone-form').style.display = 'none';
        document.getElementById('report-success').style.display = 'block';
        window.scrollTo({top: 0, behavior: 'smooth'});
    });
});
