from collections import defaultdict
import datetime
import itertools
import logging
import web

logger = logging.getLogger(__name__)

db = web.database(dbn="postgres", db="covid19")
db.printing = True

def get_thehindu_cases():
    """Returns the cases from most recent entry.
    """
    cols = "state_code, confirmed, active, deaths, recovered, indians, foreigners"
    entry_id = db.select("covid19_thehindu_entry", order="id desc", limit=1).first().id
    result = db.where("covid19_thehindu", what=cols, entry_id=entry_id)
    return {row.pop('state_code'): row for row in result}

class Source:
    def __init__(self, name):
        self.name = name
        self.table = f"covid19_{name}"
        self.entry_table = self.table + "_entry"

    def has_entry(self, timestamp):
        entry = db.where(self.entry_table, timestamp=timestamp).first()
        return entry is not None

    def add_new_entry(self, timestamp, data):
        if self.has_entry(timestamp):
            logger.info("entry already exists for %s. ignoring...", timestamp)
            return
        logger.info("adding entry for %s", timestamp)
        with db.transaction():
            entry_id = db.insert(self.entry_table, timestamp=timestamp)
            data = [dict(row, entry_id=entry_id) for row in data]
            db.multiple_insert(self.table, data)

    def get_entry(self, date=None):
        if date:
            date = datetime.datetime.strptime(date, "%Y-%m-%d") + datetime.timedelta(days=1)
            where = "timestamp < $date"
            vars = {"date": date}
        else:
            where = "1 = 1"
            vars = {}
        
        return db.select(
            self.entry_table, 
            vars=vars, 
            where=where, 
            order="timestamp desc", 
            limit=1).first()

    def get_cases(self, date=None):
        cols = "state_code, confirmed, active, deaths, recovered, indians, foreigners"
        entry = self.get_entry(date)
        result = db.where(self.table, what=cols, entry_id=entry.id)
        cases = {row.pop('state_code'): row for row in result}
        return {
            "source": self.name,
            "last_updated": entry.timestamp.isoformat(),
            "states": cases,
            "india": self.compute_totals(cases)
        }

    def compute_totals(self, cases):
        # keys of the any one entry
        k = list(cases.keys())[0]
        keys = cases[k].keys()
        return {k: sum(case[k] or 0 for case in cases.values()) for k in keys}

    def get_daily_entries(self):
        return db.query(f"""
            SELECT DISTINCT ON (date_trunc('day', timestamp))
            id, timestamp
            FROM {self.entry_table}
            ORDER BY date_trunc('day', timestamp) DESC, timestamp DESC""")

    def get_daily_cases(self, state=None, metric="confirmed"): 
        entries = self.get_daily_entries()
        entry_ids = [e.id for e in entries]
        if state:
            where = " AND state_code=$state"
        else:
            where = ""
        result = db.query(f"""
            SELECT timestamp, 
                sum(confirmed) as confirmed, 
                sum(active) as active, 
                sum(deaths) as deaths, 
                sum(recovered) as recovered,
                sum(indians) as indians, 
                sum(foreigners) as foreigners
            FROM {self.table}
            JOIN {self.entry_table} ON {self.entry_table}.id = {self.table}.entry_id
            WHERE entry_id IN $entry_ids {where}
            GROUP BY timestamp
            ORDER BY timestamp DESC
            """, 
            vars={"entry_ids": entry_ids, "state": state})

        def process_case(case):
            case['date'] = case.timestamp.date().isoformat()
            case['timestamp'] = case.timestamp.isoformat()
            return case

        return {
            "source": self.name,
            "cases": [process_case(case) for case in result]
        }

    def group_by_state(self, cases):
        def process_case(case):
            case['date'] = case.timestamp.date().isoformat()
            case['timestamp'] = case.timestamp.isoformat()
            del case['state_code']
            return case

        return {state: [process_case(case) for case in state_cases]
                for state, state_cases in
                itertools.groupby(cases, lambda case: case['state_code'])}

    def get_cases_growth(self):
        cols = 'state_code,confirmed'
        entries = self.get_daily_entries()        
        entry_ids = [e.id for e in entries]

        result = db.query(f"""
            SELECT date_trunc('day', timestamp)::date as date, state_code as state, confirmed
            FROM {self.table}
            JOIN {self.entry_table} ON {self.entry_table}.id = {self.table}.entry_id
            WHERE entry_id IN $entry_ids 
            ORDER BY 2, 1 DESC
            """, 
            vars={"entry_ids": entry_ids})

        states = defaultdict(list)
        for row in result:
            states[row.state].append(row)

        max_date = max({row.date for rows in states.values() for row in rows})
        dates = [max_date-i*datetime.timedelta(days=1) for i in range(21)][:-1]

        def compute_totals():
            counts = defaultdict(list)
            for rows in states.values():
                for row in rows:
                    counts[row.date].append(row.confirmed)
            return [web.storage(date=date, confirmed=sum(counts[date])) for date in dates]

        def process_growth(key, rows):
            doubled_in = self._compute_doubled_in([row.confirmed for row in rows])
            cases = {row.date: row.confirmed for row in rows}
            return {
                "state": key,
                "doubled_in": doubled_in,
                "cases": [dict(date=date.isoformat(), cases=cases.get(date, 0)) for date in dates],
                "current": cases.get(max_date, 0)
            }
        states['india'] = compute_totals()
        return sorted([process_growth(key, rows) for key, rows in states.items()], reverse=True, key=lambda x: x['cases'][0]['cases'])

    def _compute_doubled_in(self, values):
        current = values[0]
        try:
            return first(i for i, v in enumerate(values) if v and v+v < current)
        except StopIteration:
            return None

def first(seq):
    return next(iter(seq))