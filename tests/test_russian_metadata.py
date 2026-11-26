"""Russian clip metadata stays in Cyrillic, while game terms may stay Latin."""
import os, shutil, sys, tempfile
data = tempfile.mkdtemp(prefix='pc-ru-meta-')
os.environ['PEAKCUT_DATA'] = data; os.environ['DATABASE_URL'] = ''
sys.path.insert(0, '.')
from studio import brain

fails = []
def check(name, got, want=True):
    ok = got == want
    print(('PASS ' if ok else 'FAIL ') + name + ('' if ok else f' {got!r}'))
    if not ok: fails.append(name)

ru = {'score': 88, 'category': 'clutch', 'hook': 'Он берёт клатч на AWP', 'title': 'Клатч в CS2',
      'description': 'Игрок забирает раунд в CS2 с AWP.', 'hashtags': ['cs2'], 'highlight_words': ['клатч'], 'reason': 'Решающая победа.'}
en = dict(ru, hook='He wins the clutch', title='Amazing CS2 clutch', description='He wins with an AWP.', reason='A big win.')
latin = dict(ru, hook='On beret klatch', title='Klatch v CS2', description='Igrok zabiraet raund.', reason='Reshayushchaya pobeda.')
check('Russian metadata accepted', brain.metadata_valid(ru, 'ru'))
check('English metadata rejected', brain.metadata_valid(en, 'ru'), False)
check('transliterated metadata rejected', brain.metadata_valid(latin, 'ru'), False)
check('English metadata unchanged', brain.metadata_valid(en, 'en'))
check('Russian locale metadata accepted', brain.metadata_valid(ru, 'ru-RU'))
calls = []
brain.ask_ai = lambda *a, **k: (calls.append(k.get('purpose')) or ru, 'm', 1)
fixed = brain._metadata(en, 4, 19, lang='ru', transcript='Он берёт клатч на AWP в CS2')
check('one metadata-only repair is requested', calls, ['Clip: Russian metadata repair'])
check('repair preserves score and boundaries', (fixed['score'], fixed['start'], fixed['end']), (88, 4, 19))
check('repair returns Russian fields', brain.metadata_valid(fixed, 'ru'))
brain.ask_ai = lambda *a, **k: (_ for _ in ()).throw(TimeoutError('timeout'))
fallback = brain._metadata(en, 4, 19, lang='ru', transcript='Он берёт клатч на AWP в CS2')
check('timeout gets deterministic Russian fallback', (fallback['title'], fallback['score'], fallback['start'], fallback['end']),
      ('Лучший момент стрима', 88, 4, 19))
prompts = []
def answer(prompt, *args, **kwargs):
    prompts.append((prompt, kwargs.get('purpose')))
    candidate = dict(en, start=0, end=20)
    return (candidate if kwargs.get('purpose') == 'Live: words judge' else ru, 'm', 1)
brain.ask_ai = answer
words = [{'s': n * 2, 'e': n * 2 + 1, 'w': text} for n, text in enumerate(
    'Он берёт клатч на AWP и выигрывает раунд в CS2 прямо сейчас'.split())]
clips, _ = brain.judge_transcript(words, 0, 24, min_len=8, max_len=20, lang='ru')
check('live judge receives Russian output instruction', 'output language are Russian' in prompts[0][0])
check('live judge repair stays metadata-only', (clips[0]['score'], clips[0]['start'], clips[0]['end']), (88, 0, 19.35))
check('live judge returns Russian metadata', brain.metadata_valid(clips[0], 'ru'))
brain.ask_ai = lambda prompt, *a, **k: (ru, 'm', 1)
manual = brain.describe('Он берёт клатч на AWP и выигрывает важный раунд в CS2', lang='ru')
post = brain.post_text('Он берёт клатч на AWP и выигрывает важный раунд в CS2', lang='ru')
check('manual metadata keeps Russian output', brain.metadata_valid(manual, 'ru'))
check('regenerated post text keeps Russian output', bool(post['title']) and bool(post['description']))
print('ALL PASSED' if not fails else f'{len(fails)} FAILED')
shutil.rmtree(data)
sys.exit(1 if fails else 0)
