// ===== FLIGHT DISPATCH BOARD — Apps Script backend =====

const FLEET   = ['M2-2032', 'M2-2034', 'M2-2038'];
const HEADERS = ['Aircraft', 'Airworthy', 'Location', 'Snag', 'MX', 'Updated', 'Updated By'];

function sheets_() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  let status = ss.getSheetByName('Status');
  if (!status) {
    status = ss.insertSheet('Status');
    status.appendRow(HEADERS);
    FLEET.forEach(id => status.appendRow([id, 'Y', 'HQ - Boundary Row', '', '', '', '']));
  }
  if (status.getLastColumn() < 7) status.getRange(1, 7).setValue('Updated By');
  let log = ss.getSheetByName('Log');
  if (!log) {
    log = ss.insertSheet('Log');
    log.appendRow(['Timestamp', 'Aircraft', 'Field', 'Old', 'New', 'By']);
  }
  return { status, log };
}

function doGet() {
  return HtmlService.createHtmlOutputFromFile('board')
    .setTitle('Flight Dispatch Board')
    .setFaviconUrl('https://raw.githubusercontent.com/timrundlespiers/DISPATCH/main/icon_192.png')
    .addMetaTag('viewport', 'width=device-width, initial-scale=1.0')
    .setXFrameOptionsMode(HtmlService.XFrameOptionsMode.ALLOWALL);
}

function getHangar() {
  const { status } = sheets_();
  const rows = status.getDataRange().getValues();
  const out = {};
  for (let i = 1; i < rows.length; i++) {
    const [id, aw, loc, snag, mx] = rows[i];
    if (!id) continue;
    out[id] = {
      airworthy: String(aw).toUpperCase() === 'Y',
      location: loc || '',
      snag: snag || '',
      mx: mx || ''
    };
  }
  return out;
}

function setHangarField(aircraft, field, value) {
  const { status, log } = sheets_();
  const rows = status.getDataRange().getValues();
  const colIndex = { airworthy: 1, location: 2, snag: 3, mx: 4 };
  if (!(field in colIndex)) return { ok: false, error: 'bad field' };
  let newVal = value;
  if (field === 'airworthy') newVal = value ? 'Y' : 'N';
  const who = Session.getActiveUser().getEmail() || 'unknown';
  const now = new Date();
  for (let i = 1; i < rows.length; i++) {
    if (rows[i][0] !== aircraft) continue;
    const col = colIndex[field];
    const oldVal = rows[i][col];
    if (String(oldVal) !== String(newVal)) {
      status.getRange(i + 1, col + 1).setValue(newVal);
      status.getRange(i + 1, 6).setValue(now);
      status.getRange(i + 1, 7).setValue(who);
      log.appendRow([now, aircraft, field, oldVal, newVal, who]);
    }
    return { ok: true };
  }
  return { ok: false, error: 'aircraft not found' };
}

// ===== weather + NOTAM from Drive files =====
const DISPATCH_FOLDER_ID = '161JRFF0KfXfMWX_l6Gm7Tnb7RPtjQR10';

function readDriveFile_(name) {
  try {
    const folder = DriveApp.getFolderById(DISPATCH_FOLDER_ID);
    const it = folder.getFilesByName(name);
    if (it.hasNext()) return it.next().getBlob().getDataAsString();
  } catch (e) {}
  return '';
}

function getWeather() {
  const txt = readDriveFile_('qnh.js');
  const m = txt.match(/window\.LOCAL_QNH\s*=\s*(\{[\s\S]*\});?\s*$/);
  if (!m) return null;
  try { return JSON.parse(m[1]); } catch (e) { return null; }
}

function getRoutes() {
  const txt = readDriveFile_('routes.js');
  const m = txt.match(/window\.LOCAL_ROUTES\s*=\s*(\{[\s\S]*\});?\s*$/);
  if (!m) return null;
  try { return JSON.parse(m[1]); } catch (e) { return null; }
}

// ===== BATTERIES =====
const BATTERY_FIELDS = ['total', 'airworthy', 'guys', 'gosh', 'hq'];

function batterySheet_() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  let b = ss.getSheetByName('Batteries');
  if (!b) {
    b = ss.insertSheet('Batteries');
    b.appendRow(['Field', 'Value']);
    BATTERY_FIELDS.forEach(f => b.appendRow([f, 0]));
  }
  return b;
}

function getBatteries() {
  const b = batterySheet_();
  const rows = b.getDataRange().getValues();
  const out = {};
  for (let i = 1; i < rows.length; i++) {
    if (rows[i][0]) out[rows[i][0]] = Number(rows[i][1]) || 0;
  }
  BATTERY_FIELDS.forEach(f => { if (!(f in out)) out[f] = 0; });
  return out;
}

function setBattery(field, value) {
  if (BATTERY_FIELDS.indexOf(field) === -1) return { ok: false, error: 'bad field' };
  const b = batterySheet_();
  const rows = b.getDataRange().getValues();
  const newVal = Number(value) || 0;
  const who = Session.getActiveUser().getEmail() || 'unknown';
  const now = new Date();
  for (let i = 1; i < rows.length; i++) {
    if (rows[i][0] === field) {
      const oldVal = rows[i][1];
      if (String(oldVal) !== String(newVal)) {
        b.getRange(i + 1, 2).setValue(newVal);
        const { log } = sheets_();
        log.appendRow([now, 'BATTERY', field, oldVal, newVal, who]);
      }
      return { ok: true };
    }
  }
  b.appendRow([field, newVal]);
  return { ok: true };
}
