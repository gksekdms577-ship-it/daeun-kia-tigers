#!/usr/bin/env python3
"""Refresh box scores of recent KIA games using public KBO Game Center endpoints.
Existing verified records are retained if upstream services fail. No fabricated stats.
"""
import datetime as dt
import json
import os
import time
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.parse import urlencode
import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / 'lineup-data.json'
SCHEDULE = 'https://raw.githubusercontent.com/dentearl/kbo-data/main/data/kbo/2026.json'
BOX = 'https://www.koreabaseball.com/ws/Schedule.asmx/GetBoxScoreScroll'
PREVIEW = 'https://www.koreabaseball.com/ws/Schedule.asmx/GetLineUpAnalysis'

def get_json(url, data=None):
    headers = {'User-Agent':'Mozilla/5.0 (compatible; TigersFanPage/1.0)', 'Accept':'application/json'}
    if data is not None:
        headers.update({'Content-Type':'application/x-www-form-urlencoded; charset=UTF-8',
                        'X-Requested-With':'XMLHttpRequest',
                        'Referer':'https://www.koreabaseball.com/Schedule/GameCenter/Main.aspx'})
        data=urlencode(data).encode('utf-8')
    with urlopen(Request(url,data=data,headers=headers),timeout=25) as response:
        return json.load(response)

def rows(table):
    try:
        obj = json.loads(table) if isinstance(table,str) else (table or {})
        return [[str(cell.get('Text','')).replace('&nbsp;','').strip() for cell in row.get('row',[])]
                for row in obj.get('rows',[])]
    except (ValueError,AttributeError,TypeError):
        return []

def hitters(obj):
    starts=rows(obj.get('table1',''))
    stats=rows(obj.get('table3',''))
    result=[]
    for i,line in enumerate(starts):
        if len(line)<3 or not line[2]:continue
        order,position,name=line[:3]
        # Non-numeric batting order rows are substitutions, not starting batters.
        if not order.strip().isdigit():continue
        player={'order':order,'position':position,'name':name}
        if i<len(stats) and len(stats[i])>=5 and stats[i][4]:
            player['avg']=stats[i][4] # game-center displayed average, not necessarily pregame season AVG
        result.append(player)
    return result

def pitchers(obj):
    result=[]
    names=['name','entry','result','W','L','SV','IP','TBF','NP','AB','H','HR','BB','SO','R','ER','ERA']
    for line in rows(obj.get('table','')):
        p=dict(zip(names,line))
        if not p.get('name'):continue
        record={'name':p['name']}
        for key,field in [('R','runs'),('ER','earnedRuns'),('ERA','era'),('IP','innings'),('entry','entry')]:
            if p.get(key):record[field]=p[key]
        result.append(record)
    return result

def preview_team(raw, meta_idx, lineup_idx):
    meta=(raw.get(str(meta_idx)) or [{}])[0] or {}
    entries=raw.get(str(lineup_idx)) or []
    table=entries[0] if entries else ''
    players=[]
    for line in rows(table):
        if len(line)<3 or not line[0].isdigit() or not line[2]:continue
        player={'order':line[0],'position':line[1],'name':line[2]}
        if len(line)>3 and line[3]:player['war']=line[3]
        players.append(player)
    return {'teamId':meta.get('T_ID',''), 'hitters':players, 'pitchers':[]}

def fetch_preview(pk, form, kia_away, today):
    raw=get_json(PREVIEW,form)
    if raw.get('code')=='200' or not isinstance(raw.get('0'),list):return None
    flag=bool((raw['0'][0] or {}).get('LINEUP_CK'))
    if not flag:return None # Never label a last-game projected lineup as today's starters.
    home=preview_team(raw,1,3)
    away=preview_team(raw,2,4)
    kia,opp=(away,home) if kia_away else (home,away)
    if len(kia['hitters'])<9:return None
    return {'kia':kia,'opponent':opp, 'source':'KBO Game Center GetLineUpAnalysis',
            'lineupStatus':'confirmed', 'updated':today,
            'avgLabel':'발표된 라인업(타율은 별도 제공되지 않음)',
            'pitcherLabel':'선발투수 및 투수기록은 별도 제공되지 않음'}

# KBO official season player-record pages. ASP.NET pagination and year/team
# filters are submitted using the page's actual form fields, not guessed values.
STAT_PAGES = {
    'hitter':'https://www.koreabaseball.com/Record/Player/HitterBasic/Basic1.aspx',
    'pitcher':'https://www.koreabaseball.com/Record/Player/PitcherBasic/Basic1.aspx',
}

def _form_fields(soup):
    form=soup.find('form',id='aspnetForm') or soup.find('form')
    if not form:return {}
    result={}
    for inp in form.select('input[name]'):
        if inp.get('type','').lower() in ('submit','button','image','file','checkbox','radio'):continue
        result[inp['name']]=inp.get('value','')
    for select in form.select('select[name]'):
        option=select.find('option',selected=True) or select.find('option')
        if option:result[select['name']]=option.get('value','')
    return result

def _player_rows(soup,role):
    out={}
    for tr in soup.select('table tbody tr'):
        cells=[c.get_text(' ',strip=True) for c in tr.find_all('td',recursive=False)]
        if len(cells)<15:continue
        player=tr.find('a',href=lambda h:h and 'playerId=' in h)
        if not player:continue
        name=player.get_text(' ',strip=True)
        team=cells[2].upper() if len(cells)>2 else ''
        if not name or team not in ('KIA','기아'):continue
        # Standard KBO basic batting columns: rank, player, team, AVG, G ...
        # Standard pitching columns: rank, player, team, ERA, G, ... R, ER.
        rec={'name':name}
        if role=='hitter':
            rec.update(avg=cells[3],games=cells[4])
        else:
            rec.update(era=cells[3],games=cells[4])
            heads=[th.get_text(' ',strip=True).upper() for th in tr.find_parent('table').select('thead th')]
            for label,field in [('R','runs'),('ER','earnedRuns'),('GS','starts')]:
                if label in heads and heads.index(label)<len(cells):rec[field]=cells[heads.index(label)]
        if 'playerId=' in player.get('href',''):
            rec['playerId']=player['href'].split('playerId=')[-1].split('&')[0]
        out[name]=rec
    return out

def fetch_season_stats(role):
    url=STAT_PAGES[role]
    session=requests.Session()
    session.headers.update({'User-Agent':'Mozilla/5.0','Referer':url})
    response=session.get(url,timeout=22);response.raise_for_status()
    soup=BeautifulSoup(response.text,'html.parser')
    # Changing team via the site's real dropdown reduces pagination and avoids
    # matching a same-name player on a different team.
    selects=soup.select('select[name]')
    team_select=next((x for x in selects if 'ddlTeam' in x.get('name','')),None)
    if team_select:
        option=next((x for x in team_select.select('option') if x.get('value')=='HT' or x.get_text(' ',strip=True).upper()=='KIA'),None)
        if option:
            fields=_form_fields(soup)
            fields[team_select['name']]=option.get('value','HT')
            fields['__EVENTTARGET']=team_select['name']
            fields['__EVENTARGUMENT']=''
            reply=session.post(url,data=fields,timeout=22);reply.raise_for_status()
            soup=BeautifulSoup(reply.text,'html.parser')
    out=_player_rows(soup,role)
    # Traverse ASP.NET pager using __EVENTTARGET from the actual pager links.
    seen=set()
    for _ in range(25):
        links=[]
        for a in soup.select('a[href*="__doPostBack"]'):
            import re
            match=re.search(r"__doPostBack\(['\"]([^'\"]+)['\"],\s*['\"]([^'\"]*)",a.get('href',''))
            if match and ('Page$' in match.group(2) or 'Page$' in match.group(1)):
                links.append((match.group(1),match.group(2)))
        next_page=next(((target,arg) for target,arg in links if (target,arg) not in seen and arg.startswith('Page$')),None)
        if not next_page:break
        seen.add(next_page)
        fields=_form_fields(soup)
        fields['__EVENTTARGET']=next_page[0]
        fields['__EVENTARGUMENT']=next_page[1]
        reply=session.post(url,data=fields,timeout=22);reply.raise_for_status()
        next_soup=BeautifulSoup(reply.text,'html.parser')
        added=_player_rows(next_soup,role)
        if not added or all(n in out for n in added):break
        out.update(added)
        soup=next_soup
    print('SEASON STATS',role,len(out),'KIA players')
    return out

def enrich_team(team,hitters_by_name,pitchers_by_name):
    for player in team.get('hitters',[]):
        source=hitters_by_name.get(player['name'])
        if source:
            for field in ('avg','games'):
                if source.get(field):player[field]=source[field]
            player['seasonStatsSource']='KBO 공식 시즌 기록'
    for player in team.get('pitchers',[]):
        source=pitchers_by_name.get(player['name'])
        if source:
            for field in ('games','starts','era'):
                if source.get(field):
                    if field in ('runs','earnedRuns','era') and player.get(field):
                        player['season'+field[0].upper()+field[1:]]=source[field]
                    else:player[field]=source[field]
            player['seasonStatsSource']='KBO 공식 시즌 기록'

def has_content(team):return bool(team['hitters'] or team['pitchers'])

def main():
    saved={}
    if TARGET.exists():
        try:saved=json.loads(TARGET.read_text(encoding='utf-8'))
        except (ValueError,OSError):saved={}
    games=get_json(SCHEDULE)
    today=(dt.datetime.now(dt.timezone(dt.timedelta(hours=9)))).date().isoformat()
    selected=[g for g in games if g.get('officialDate','')<=today
              and any(g.get('teams',{}).get(side,{}).get('team',{}).get('abbrev')=='KIA' for side in ('away','home'))]
    selected.sort(key=lambda x:(x.get('officialDate',''),x.get('gamePk','')),reverse=True)
    try:season_hitters=fetch_season_stats('hitter')
    except Exception as e:print('SEASON HITTERS unavailable:',repr(e));season_hitters={}
    try:season_pitchers=fetch_season_stats('pitcher')
    except Exception as e:print('SEASON PITCHERS unavailable:',repr(e));season_pitchers={}
    successes=0
    for g in selected[:12]:
        pk=str(g.get('gamePk',''))
        if len(pk)<13:continue
        is_final=g.get('status',{}).get('abstractGameState')=='F'
        existing=saved.get(pk,{})
        if is_final and existing.get('source')=='KBO Game Center box score':
            enrich_team(existing.get('kia',{}),season_hitters,season_pitchers)
            continue
        form={'leId':1,'srId':0,'seasonId':2026,'gameId':pk}
        kia_away=g['teams']['away']['team']['abbrev']=='KIA'
        try:
            if not is_final:
                # Call the official lineup feed for today's game before the first pitch.
                # The feed can contain *projected* last-game data; store confirmed only.
                if g.get('officialDate','')!=today:continue
                record=fetch_preview(pk,form,kia_away,today)
                if record:
                    enrich_team(record['kia'],season_hitters,season_pitchers)
                    saved[pk]=record
                    successes+=1
                    print('CONFIRMED LINEUP',pk,len(record['kia']['hitters']))
                else:
                    print('NOT YET ANNOUNCED',pk)
            else:
                data=get_json(BOX,form)
                h=data.get('arrHitter',[]);p=data.get('arrPitcher',[])
                if len(h)<2 and len(p)<2:raise ValueError('empty box score')
                away={'hitters':hitters(h[0]) if len(h)>0 else [],'pitchers':pitchers(p[0]) if len(p)>0 else []}
                home={'hitters':hitters(h[1]) if len(h)>1 else [],'pitchers':pitchers(p[1]) if len(p)>1 else []}
                kia,opp=(away,home) if kia_away else (home,away)
                if not has_content(kia):raise ValueError('no KIA player rows')
                enrich_team(kia,season_hitters,season_pitchers)
                saved[pk]={'kia':kia,'opponent':opp,'source':'KBO Game Center box score',
                           'lineupStatus':'postgame','updated':today,'avgLabel':'KBO 박스스코어 표시 시즌 타율',
                           'pitcherLabel':'해당 경기 투수 기록'}
                successes+=1
                print('BOX SCORE',pk,len(kia['hitters']),'batters')
        except Exception as e:
            print('SKIP',pk,str(e))
        time.sleep(0.5)
    # Don't replace verified data with an empty response. A zero-success run shouldn't fail deployment.
    TARGET.write_text(json.dumps(saved,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print('Updated',successes,'games. Stored',len(saved),'games.')

if __name__=='__main__':main()
