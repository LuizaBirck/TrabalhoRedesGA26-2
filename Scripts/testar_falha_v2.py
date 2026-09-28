#!/usr/bin/env python3
"""Teste de falha R2-R5: sudo python3 testar_falha_v2.py bgp

Uma repeticao exploratoria de 240 s, com restauracao automatica do enlace.
Mede recuperacao observada H1-H5, nao convergencia de todas as tabelas.
Probes sequenciais: intervalo minimo de 0.2 s; timeout de 1 s por ping.
Nao desligue ou suspenda a VM durante o teste. Ctrl+C restaura o enlace.
"""
import argparse
import csv
import datetime
import json
import os
from pathlib import Path
import pwd
import shutil
import subprocess
import sys
import time

ENV = dict(os.environ, LC_ALL='C', LANG='C')
DAEMONS = {'rip': 'ripd', 'ospf': 'ospfd', 'bgp': 'bgpd'}
ENDS = (('lab-r2', 'r2-r5'), ('lab-r5', 'r5-r2'))


def run(args, timeout=40):
    result = subprocess.run(args, text=True, capture_output=True, env=ENV, timeout=timeout)
    if result.returncode:
        raise RuntimeError(' '.join(args) + '\n' + result.stdout + result.stderr)
    return result.stdout


def ns(name, *args):
    return ['ip', 'netns', 'exec', name, *args]


def link(state):
    for namespace, interface in ENDS:
        run(['ip', '-n', namespace, 'link', 'set', 'dev', interface, state])


def snapshot(folder, tag):
    decisions = {}
    for n in range(1, 6):
        name = f'lab-r{n}'
        (folder / f'{tag}_r{n}_rotas.txt').write_text(
            run(ns(name, 'vtysh', '-N', name, '-c', 'show ip route')))
        # Consulta a FIB: traceroute pode omitir respostas ICMP intermediarias.
        query = subprocess.run(['ip', '-n', name, '-j', '-4', 'route', 'get', '192.168.5.10'],
                               text=True, capture_output=True, env=ENV, timeout=10)
        decisions[name] = (json.loads(query.stdout) if query.returncode == 0
                           else {'erro': query.stderr.strip()})
    (folder / f'{tag}_encaminhamento.json').write_text(json.dumps(decisions, indent=2))
    trace = run(ns('lab-h1', 'traceroute', '-n', '-w', '1', '-m', '10', '192.168.5.10'))
    (folder / f'{tag}_traceroute.txt').write_text(trace)
    return trace, decisions


def uses_test_link(decisions):
    expected = {'lab-r1': ('r1-r2', '10.0.12.2'), 'lab-r2': ('r2-r5', '10.0.25.2')}
    for name, (interface, gateway) in expected.items():
        route = decisions.get(name)
        if not isinstance(route, list) or len(route) != 1:
            return False
        if route[0].get('dev') != interface or route[0].get('gateway') != gateway:
            return False
    return True


def recovery(rows):
    failures = [i for i, r in enumerate(rows) if not r['sucesso']]
    if not failures:
        return {'interpretacao': 'Nenhuma falha de ping observada; nao registrar tempo zero.',
                'recuperacao_observada_s': None}
    index = failures[-1] + 1
    tail = rows[index:]
    if len(tail) < 5:
        return {'interpretacao': 'Recuperacao nao confirmada por cinco respostas ate o fim da janela.',
                'recuperacao_observada_s': None}
    return {'interpretacao': 'Fim da primeira sonda bem-sucedida apos a ultima falha observada; '
            'confirmada por pelo menos cinco sucessos subsequentes. Inclui custo da sondagem.',
            'recuperacao_observada_s': tail[0]['fim_s'],
            'ultima_falha_fim_s': rows[index-1]['fim_s'],
            'primeiro_sucesso_inicio_s': tail[0]['inicio_s']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('protocolo', choices=DAEMONS)
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise RuntimeError('Use sudo python3 testar_falha_v2.py bgp')
    for n in range(1, 6):
        pids = run(['ip', 'netns', 'pids', f'lab-r{n}']).split()
        names = set()
        for pid in pids:
            try:
                names.add(Path(f'/proc/{pid}/comm').read_text().strip())
            except FileNotFoundError:
                pass
        if names & set(DAEMONS.values()) != {DAEMONS[args.protocolo]}:
            raise RuntimeError(f'Protocolo ativo inesperado no R{n}: {names}')
    for namespace, interface in ENDS:
        info = json.loads(run(['ip', '-n', namespace, '-j', 'link', 'show', 'dev', interface]))[0]
        if 'UP' not in info['flags']:
            raise RuntimeError(f'{namespace}/{interface} ja esta desligada. Restaure antes do teste.')
    uid = int(os.environ.get('SUDO_UID', os.getuid()))
    gid = int(os.environ.get('SUDO_GID', os.getgid()))
    base = Path(pwd.getpwuid(uid).pw_dir) / 'trabalho-redes' / 'resultados'
    base.mkdir(parents=True, exist_ok=True)
    output = base / ('falha_' + args.protocolo + '_' + datetime.datetime.now().strftime('%Y%m%d_%H%M%S_%f'))
    output.mkdir()
    rows = []
    events = {}
    changed = False
    try:
        print('Verificando caminho original...', flush=True)
        run(ns('lab-h1', 'ping', '-n', '-c', '5', '-W', '1', '192.168.5.10'))
        trace, decisions = snapshot(output, 'antes')
        if not uses_test_link(decisions):
            raise RuntimeError('As consultas de encaminhamento nao confirmam R1-R2-R5. Envie o ZIP antes de testar.')
        print(trace, flush=True)
        print('Tabelas de encaminhamento confirmam R1 -> R2 -> R5.', flush=True)
        print('Desligando apenas o enlace virtual R2-R5 por 240 segundos...', flush=True)
        events['inicio_falha_epoch'] = time.time()
        zero = time.monotonic()
        changed = True
        link('down')
        events['fim_comandos_down_s'] = time.monotonic() - zero
        next_progress = 30
        with (output / 'sondas.csv').open('w', newline='') as stream, (output / 'pings_brutos.txt').open('w') as raw:
            fields = ['sonda', 'inicio_s', 'fim_s', 'sucesso']
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            while time.monotonic() - zero < 240:
                start = time.monotonic()
                probe = subprocess.run(ns('lab-h1', 'ping', '-n', '-c', '1', '-W', '1', '192.168.5.10'),
                                       env=ENV, capture_output=True, text=True, timeout=4)
                end = time.monotonic()
                if probe.returncode not in (0, 1):
                    raise RuntimeError('Erro de execucao do ping: ' + probe.stderr)
                row = dict(sonda=len(rows)+1, inicio_s=start-zero, fim_s=end-zero, sucesso=int(probe.returncode == 0))
                rows.append(row)
                writer.writerow(row)
                stream.flush()
                raw.write(json.dumps(row) + '\n' + probe.stdout + probe.stderr + '\n')
                if end-zero >= next_progress:
                    print(f'{int(end-zero)}/240 s | sondas sem resposta: {sum(not r["sucesso"] for r in rows)}', flush=True)
                    next_progress += 30
                time.sleep(max(0, 0.2-(time.monotonic()-start)))
        print('Registrando o caminho com o enlace ainda desligado...', flush=True)
        trace_after, decisions_after = snapshot(output, 'durante_falha')
        print(trace_after, flush=True)
        summary = {'protocolo': args.protocolo, 'enlace': 'R2-R5',
                   'janela_planejada_s': 240, 'sondas': len(rows),
                   'sondas_sem_resposta': sum(not r['sucesso'] for r in rows),
                   **recovery(rows)}
        (output / 'resumo.json').write_text(json.dumps(summary, indent=2, ensure_ascii=False))
        print(json.dumps(summary, indent=2, ensure_ascii=False), flush=True)
    finally:
        try:
            if changed:
                link('up')
                events['restauracao_epoch'] = time.time()
                print('ENLACE R2-R5 RELIGADO.', flush=True)
        finally:
            (output / 'eventos.json').write_text(json.dumps(events, indent=2))
            (output / 'metodo.txt').write_text(__doc__ + '\nNao comparar percentual de falhas com '
                'o percentual de um ping continuo: intervalo entre sondas cresce quando ha timeout.\n'
                'Os snapshots durante a falha sao feitos apos as sondagens, antes de religar.\n'
                'Versao 2: somente a verificacao previa mudou para consultas da FIB; '
                'as sondagens e o calculo de recuperacao mantem o mesmo metodo da versao 1.\n')
            for path in [output, *output.rglob('*')]:
                os.chown(path, uid, gid)
            archive = Path(shutil.make_archive(str(output), 'zip', root_dir=base, base_dir=output.name))
            os.chown(archive, uid, gid)
            print('ENVIE ESTE ZIP:', archive, flush=True)
    print('TESTE CONCLUIDO. O enlace esta ligado; o protocolo ainda pode precisar atualizar as rotas.', flush=True)


if __name__ == '__main__':
    try:
        main()
    except (Exception, KeyboardInterrupt) as error:
        print('TESTE INTERROMPIDO:', error, file=sys.stderr)
        sys.exit(1)
