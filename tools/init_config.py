"""从公开模板创建本地配置；已经存在的文件不会被覆盖。"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def initialize(root=ROOT):
    created = []
    for stem in ('bot_config', 'members_config', 'llm_config'):
        destination = root / (stem + '.json')
        try:
            with destination.open('x', encoding='utf-8', newline='\n') as output:
                output.write((root / (stem + '.example.json')).read_text(encoding='utf-8'))
        except FileExistsError:
            continue
        created.append(destination.name)
    seed = root / '.runtime/group_knowledge_seed.json'
    template = root / 'knowledge_seed.example.json'
    if template.is_file() and not seed.exists():
        seed.parent.mkdir(parents=True, exist_ok=True)
        with seed.open('x', encoding='utf-8', newline='\n') as output:
            output.write(template.read_text(encoding='utf-8'))
        created.append('.runtime/group_knowledge_seed.json')
    return created


if __name__ == '__main__':
    created = initialize()
    print('已生成：' + '、'.join(created) if created else '本地配置已存在，未覆盖。')
