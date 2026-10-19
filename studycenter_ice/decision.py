"""Local course candidates before interpretation; no guessed model IDs."""
import re
import unicodedata
from difflib import SequenceMatcher
from typing import Literal

from pydantic import BaseModel


def key(text):
    value=''.join(c for c in unicodedata.normalize('NFD',text.casefold()) if not unicodedata.combining(c))
    return ' '.join(re.findall(r'[^\W_]+',value))


def phonetic(text):
    value=re.sub(r'ck|qu|c(?=[aou])','k',key(text))
    words=value.split()
    words=[re.fullmatch(r'(.{2,})(?:\1)+',w)[1] if re.fullmatch(r'(.{2,})(?:\1)+',w) else w for w in words]
    return ' '.join(w for n,w in enumerate(words) if not n or w!=words[n-1])


class CourseMatch(BaseModel):
    id: int
    title: str
    semester: str
    score: float
    match: Literal['alias','title','phonetic','fuzzy']


STOP={'morgen','heute','mir','mich','eine','einen','einer','einem','der','die','das','und','fur','was','wie','soll','bitte','machen','vorbereiten','aufgabe','erinnerung','unterlagen','herunterladen'}
COURSE_TYPES={'ubung','ubungen','vorlesung','vorlesungen','seminar','seminare','praktikum','praktika',
              'kurs','kurse','lehrveranstaltung','lehrveranstaltungen','vo','ue','vu','lu','laborubung','laborubungen','ku','se','pr',
              'ws','ss','wise','sose','semester','wintersemester','sommersemester',
              'lecture','lectures','exercise','exercises','tutorial','tutorials'}


def course_identity_word(value):
    normalized=key(value)
    return normalized not in STOP|COURSE_TYPES and not re.fullmatch(r'(?:ws|ss)?(?:19|20)\d{2}[ws]?',normalized)


def course_qualifiers(text):
    words=set(key(text).split())
    types={'vo':{'vo','vorlesung','vorlesungen','lecture','lectures'}, 'ue':{'ue','ubung','ubungen','exercise','exercises'},
           'vu':{'vu'}, 'lu':{'lu','laborubung','laborubungen'}, 'ku':{'ku'}, 'se':{'se','seminar','seminare'},
           'pr':{'pr','praktikum','praktika'}}
    return {kind for kind,names in types.items() if words & names}


def qualified_matches(text, matches, courses):
    requested=course_qualifiers(text)
    # Multiple/negated qualifiers need clarification rather than a guessed LV type.
    if len(requested)!=1 or re.search(r'\b(?:nicht|kein\w*|ohne|not|no)\b',key(text)):return matches
    types={}
    for course in courses:
        declared=course_qualifiers(course.get('course_type') or '')
        # Parenthesized official abbreviations are more specific than title words.
        bracketed=set().union(*(course_qualifiers(part) for part in re.findall(r'\(([^)]*)\)',course['title'])))
        types[course['id']]=declared or bracketed or course_qualifiers(course['title'])
    if not any(types[match.id]==requested for match in matches):return matches
    # Unknown metadata remains a candidate; only positively conflicting types
    # are removed after the subject itself has already supplied identity evidence.
    return [match for match in matches if not types[match.id] or types[match.id] & requested]


def candidates(text,courses,phrase=False):
    original_query=query=key(text)
    if phrase:
        query=' '.join(word for word in query.split() if word not in COURSE_TYPES
                       and not re.fullmatch(r'(?:ws|ss)?(?:19|20)\d{2}[ws]?|nicht|kein\w*|ohne|not|no',word))
    chunks=re.findall(r'[^\W_]+(?:[-/][^\W_]+)*',text.casefold())
    chunks=[c for c in chunks if len(c)>=2 and course_identity_word(c)]
    title_query=any(course_identity_word(word) for word in query.split())
    letters=re.findall(r'[^\W_]+',query)
    found=[]
    for course in courses:
        score,reason=0.,'fuzzy'
        base=re.split(r'\(|,|\[',course['title'])[0].strip()
        names=[(course['title'],'title'),(base,'title'),*((a,'alias') for a in course.get('aliases',[]))]
        for name,kind in names:
            exact=key(name)
            if not exact or len(exact)<2:continue
            # Course format and semester decorators do not identify a subject.
            # Explicit learned aliases below still retain the user's correction.
            if not title_query or not any(course_identity_word(word) for word in exact.split()):continue
            if query==exact or (not phrase and re.search(r'(?<!\w)'+re.escape(exact)+r'(?!\w)',query)):
                trial,why=1.,kind
            elif phrase and len(query)>=4 and query in exact:
                trial,why=.98,'title'
            else:
                trial,why=0.,'fuzzy'
                for value in ([query] if phrase else chunks):
                    if phonetic(value)==phonetic(name):
                        trial,why=.97,'phonetic';break
                    if len(key(value))>=4 and len(exact)>=4:
                        ratio=SequenceMatcher(None,key(value),exact).ratio()
                        if ratio>=.82 and ratio>trial:trial,why=ratio,'fuzzy'
                    if not phrase and kind=='title' and len(key(value))>=5 and re.search(r'(?<!\w)'+re.escape(key(value))+r's?(?!\w)',exact):
                        trial,why=max(trial,.96),'title'
                if kind=='title' and ' ' not in exact:
                    for start in range(len(letters)-1):
                        joined=''.join(letters[start:start+2])
                        ratio=SequenceMatcher(None,joined,exact).ratio()
                        if ratio>=.82 and ratio>trial:trial,why=ratio,'fuzzy'
                if kind=='alias' and 2<=len(exact)<=5 and ' ' not in exact:
                    # Spoken letter names can be split by ASR (e.g. "C O N" or "CO in").
                    for start in range(len(letters)):
                        for size in (2,3):
                            parts=letters[start:start+size]
                            if len(parts)!=size or not any(len(p)<=2 for p in parts):continue
                            joined=''.join(parts)
                            if len(joined)>len(exact)+1:continue
                            ratio=SequenceMatcher(None,phonetic(joined),phonetic(name)).ratio()
                            if ratio>=.84 and .88>trial:trial,why=.88,'phonetic'
            if trial>score:score,reason=trial,why
        for alias in course.get('learned_aliases',[]):
            exact=key(alias)
            if original_query==exact or not phrase and re.search(r'(?<!\w)'+re.escape(exact)+r'(?!\w)',original_query):score,reason=1.1,'alias'
        if score:found.append(CourseMatch(id=course['id'],title=course['title'],semester=course['semester'],score=round(score,3),match=reason))
    return qualified_matches(text,sorted(found,key=lambda r:(-r.score,r.id)),courses)[:6]


def plausible(matches):
    if not matches:return []
    return [m for m in matches if m.score>=matches[0].score-.08]


def course_matches(query,courses):
    ids={m.id for m in plausible(candidates(query,courses,phrase=True))}
    return [c for c in courses if c['id'] in ids]


def creation_request(text):
    first=re.findall(r'[^\W_]+',key(text))
    head=first[1] if first and first[0]=='bitte' and len(first)>1 else first[0] if first else ''
    similar=any(SequenceMatcher(None,head,verb).ratio()>=.82 for verb in ('richte','erinnere','erstelle','notiere','lege','trage','fuge','schreibe'))
    return bool((similar or re.match(r'\s*(?:bitte\s+)?(?:richte|richter|erinnere|erstelle|notiere|lege|trage|f[uü]ge)\b',text,re.I)
                 or re.match(r'\s*(?:kannst|könntest|würdest)\s+du\b',text,re.I) and re.search(r'\b(?:einrichten|anlegen|erstellen|notieren)\b',text,re.I))
                and re.search(r'\b(?:erinnerung|aufgabe|task|todo|vorbereit|notiz|termin|ein|an)\w*\b',text,re.I))
