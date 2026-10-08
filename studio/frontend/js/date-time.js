/** Pure function module for compact table timestamp formatting and accessible time rendering. */
const QuicDataDateTime = (() => {
  function parse(value) {
    if (!value) return null;
    const date = value instanceof Date ? new Date(value.getTime()) : new Date(value);
    return Number.isNaN(date.valueOf()) ? null : date;
  }

  function parts(date, timeZone) {
    const formatter = new Intl.DateTimeFormat('en-CA', {
      timeZone: timeZone || undefined,
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
      hourCycle: 'h23',
    });
    return Object.fromEntries(
      formatter.formatToParts(date)
        .filter((item) => item.type !== 'literal')
        .map((item) => [item.type, item.value]),
    );
  }

  function timeZoneOffset(date, timeZone) {
    try {
      const formatter = new Intl.DateTimeFormat('en-US', {
        timeZone: timeZone || undefined,
        timeZoneName: 'shortOffset',
      });
      const value = formatter.formatToParts(date).find((item) => item.type === 'timeZoneName')?.value || '';
      return String(value)
        .replace(/^GMT\+0(\d):00$/, 'GMT+$1')
        .replace(/^GMT-(0\d):00$/, 'GMT-$1');
    } catch {
      return '';
    }
  }

  function compact(value, { timeZone } = {}) {
    const date = parse(value);
    if (!date) return '';
    const valueParts = parts(date, timeZone);
    if (!valueParts.year || !valueParts.month || !valueParts.day || !valueParts.hour || !valueParts.minute) return '';
    return `${valueParts.year}-${valueParts.month}-${valueParts.day} ${valueParts.hour}:${valueParts.minute}`;
  }

  function full(value, { timeZone } = {}) {
    const date = parse(value);
    if (!date) return '';
    const valueParts = parts(date, timeZone);
    if (!valueParts.year || !valueParts.month || !valueParts.day || !valueParts.hour || !valueParts.minute || !valueParts.second) {
      return '';
    }
    const offset = timeZoneOffset(date, timeZone);
    return `${valueParts.year}-${valueParts.month}-${valueParts.day} ${valueParts.hour}:${valueParts.minute}:${valueParts.second}${offset ? ` ${offset}` : ''}`;
  }

  return Object.freeze({ parse, compact, full });
})();

if (typeof globalThis !== 'undefined') globalThis.QuicDataDateTime = QuicDataDateTime;
