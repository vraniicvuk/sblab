"""Normalize BetBurger responses without treating provider signals as confirmed bets."""
from datetime import datetime, timezone
import math


def _timestamp(value):
    if value is None:
        return None
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()
        text = str(value)
        return datetime.fromisoformat(text.replace('Z', '+00:00')).astimezone(timezone.utc).isoformat()
    except (ValueError, TypeError, OSError, OverflowError):
        return None


def normalize_response(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get('arbs'), list) or not isinstance(payload.get('bets'), list):
        raise ValueError('Nepoznat format odgovora: očekivane su liste arbs i bets')
    bets = {str(b['id']): b for b in payload['bets'] if isinstance(b, dict) and b.get('id') is not None}
    result = []
    for signal in payload['arbs']:
        if not isinstance(signal, dict):
            continue
        ids = [str(signal[f'bet{i}_id']) for i in (1, 2, 3) if signal.get(f'bet{i}_id')]
        legs = []
        for bet_id in ids:
            b = bets.get(bet_id)
            if not b:
                continue
            try:
                odds = float(b.get('koef'))
                if not math.isfinite(odds) or odds <= 1:
                    odds = None
            except (TypeError, ValueError):
                odds = None
            legs.append({'id':bet_id, 'bookmaker_id':b.get('bookmaker_id'), 'odds':odds,
                'period':b.get('period_identifier'), 'market_type':b.get('market_and_bet_type'),
                'line':b.get('market_and_bet_type_param'), 'updated_at':_timestamp(b.get('updated_at')),
                'is_lay':b.get('is_lay'), 'market_depth':b.get('market_depth')})
        reasons = ['Pravila i dostupnost kod kladionica nisu potvrđeni', 'Tačan domen nije potvrđen']
        if len(ids) < 2 or len(legs) != len(ids): reasons.append('Nedostaje jedna ili više strana')
        if any(x['odds'] is None for x in legs): reasons.append('Nevažeća ili nepotvrđena kvota')
        if signal.get('paused') is True: reasons.append('Izvor označava događaj kao pauziran')
        result.append({'source':'betburger', 'id':str(signal.get('id') or ''),
            'home':signal.get('home'), 'away':signal.get('away'), 'is_live':signal.get('is_live'),
            'percent_provider':signal.get('percent'), 'updated_at':_timestamp(signal.get('updated_at')),
            'legs':legs, 'state':'NEPOTVRĐENO', 'reasons':reasons})
    return result
