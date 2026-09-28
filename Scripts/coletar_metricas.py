#!/usr/bin/env python3
"""Coleta real de estado estavel no laboratorio lab-r1..r5 / lab-h1..h5.

Uso: sudo python3 coletar_metricas.py rip
Repita com ospf ou bgp somente depois de trocar e validar o protocolo ativo.
Tres janelas de 60 s; seis enlaces, uma ponta por enlace, ambas as direcoes.
Contagem de quadros e bytes observados (sem preambulo/IFG/FCS ausente).
Nao mede throughput de dados nem convergencia. RTT nao e atraso unidirecional.
"""
import argparse
import csv
import datetime
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import signal
import struct
import subprocess
import sys
import time

LINKS = ('12', '13', '24', '34', '45', '25')
FILTERS = {'rip': 'udp port 520', 'ospf': 'ip proto 89', 'bgp': 'tcp port 179'}
DAEMONS = {'rip': 'ripd', 'ospf': 'ospfd', 'bgp': 'bgpd'}
ENV = dict(os.environ, LC_ALL='C', LANG='C')


def run(args, allowed=(0,)):
    result = subprocess.run(args, capture_output=True, text=True, env=ENV, timeout=50)
    if result.returncode not in allowed:
        raise RuntimeError(' '.join(args) + '\n' + result.stdout + result.stderr)
    return result.stdout


def ns(name, *args):
    return ['ip', 'netns', 'exec', name, *args]


def pcap_stats(path, start, end):
    formats = {b'\xd4\xc3\xb2\xa1': ('<', 1e6), b'\xa1\xb2\xc3\xd4': ('>', 1e6),
               b'\x4d\x3c\xb2\xa1': ('<', 1e9), b'\xa1\xb2\x3c\x4d': ('>', 1e9)}
    packets = size = 0
    with path.open('rb') as stream:
        head = stream.read(24)
        if len(head) != 24 or head[:4] not in formats:
            raise RuntimeError(f'PCAP invalido: {path}')
        endian, scale = formats[head[:4]]
        if struct.unpack(endian + 'I', head[20:24])[0] != 1:
            raise RuntimeError('Esperava captura Ethernet em interface veth.')
        while True:
            record = stream.read(16)
            if not record:
                break
            if len(record) != 16:
                raise RuntimeError('Registro PCAP incompleto')
            sec, frac, included, original = struct.unpack(endian + 'IIII', record)
            if len(stream.read(included)) != included:
                raise RuntimeError('Pacote PCAP incompleto')
            if start <= sec + frac / scale < end:
                packets += 1
                size += original
    return packets, size


def capture(folder, protocol, seconds=60):
    jobs = []
    try:
        for link in LINKS:
            a, b = link
            logfile = folder / f'enlace_{link}.log'
            stream = logfile.open('w')
            args = ns('lab-r' + a, 'tcpdump', '-n', '-U', '-s', '0', '-Z', 'root',
                      '-i', f'r{a}-r{b}', '-w', str(folder / f'enlace_{link}.pcap'), FILTERS[protocol])
            proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=stream, env=ENV)
            jobs.append((link, proc, stream, logfile))
        deadline = time.monotonic() + 10
        while True:
            if any(p.poll() is not None for _, p, _, _ in jobs):
                raise RuntimeError('tcpdump encerrou antes da coleta; confira os arquivos .log.')
            if all('listening on' in log.read_text() for _, _, _, log in jobs):
                break
            if time.monotonic() > deadline:
                raise RuntimeError('A captura nao ficou pronta em 10 segundos.')
            time.sleep(0.1)
        start = time.time()
        target = time.monotonic() + seconds
        for step in range(1, 5):
            time.sleep(max(0, target - seconds + step * seconds / 4 - time.monotonic()))
            print(f'  {step * seconds // 4}/{seconds} segundos', flush=True)
        end = time.time()
        if abs((end - start) - seconds) > 2:
            raise RuntimeError('Janela interrompida ou relogio alterado. Repita sem suspender a VM.')
    finally:
        for _, proc, _, _ in jobs:
            if proc.poll() is None:
                proc.send_signal(signal.SIGINT)
        for _, proc, stream, _ in jobs:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            stream.close()
    rows = []
    for link, proc, _, logfile in jobs:
        log = logfile.read_text()
        drops = re.search(r'(\d+) packets dropped by kernel', log)
        if proc.returncode != 0 or not drops or int(drops[1]) != 0:
            raise RuntimeError(f'Captura incompleta ou com perdas: {logfile}')
        packets, size = pcap_stats(folder / f'enlace_{link}.pcap', start, end)
        rows.append({'enlace': link, 'pacotes': packets, 'bytes_quadros': size,
                     'duracao_s': end-start, 'bits_por_segundo': size*8/(end-start)})
    (folder / 'janela.json').write_text(json.dumps({'inicio_epoch': start, 'fim_epoch': end,
        'filtro': FILTERS[protocol], 'direcoes': 'ambas'}, indent=2))
    write_csv(folder / 'controle_por_enlace.csv', rows)
    return rows


def write_csv(path, rows):
    with path.open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def snapshot(folder, protocol):
    rows = []
    for n in range(1, 6):
        name = f'lab-r{n}'
        routes = json.loads(run(['ip', '-n', name, '-j', '-4', 'route', 'show', 'table', 'main']))
        (folder / f'r{n}_rotas.json').write_text(json.dumps(routes, indent=2))
        text = run(ns(name, 'vtysh', '-N', name, '-c', 'show ip route'))
        (folder / f'r{n}_rotas.txt').write_text(text)
        destinations = {r.get('dst', 'default') for r in routes}
        lans = sum(f'192.168.{k}.0/24' in destinations for k in range(1, 6))
        if lans != 5:
            raise RuntimeError(f'R{n} tem {lans}/5 redes de acesso. Valide as rotas antes de medir.')
        rows.append({'roteador': f'R{n}', 'prefixos_ipv4_tabela_main': len(destinations),
                     'redes_acesso_presentes': lans})
    write_csv(folder / 'tabelas.csv', rows)
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('protocolo', choices=FILTERS)
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise RuntimeError('Execute com sudo python3 coletar_metricas.py rip')
    uid = int(os.environ.get('SUDO_UID', os.getuid()))
    gid = int(os.environ.get('SUDO_GID', os.getgid()))
    userdir = Path(pwd.getpwuid(uid).pw_dir)
    parent = userdir / 'trabalho-redes' / 'resultados'
    parent.mkdir(parents=True, exist_ok=True)
    os.chown(parent.parent, uid, gid)
    os.chown(parent, uid, gid)
    stamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    output = parent / f'{args.protocolo}_{stamp}'
    output.mkdir()
    print('Pasta de resultados:', output, flush=True)
    try:
        # Verifica exclusividade do protocolo nos cinco roteadores.
        for n in range(1, 6):
            name = f'lab-r{n}'
            pids = run(['ip', 'netns', 'pids', name]).split()
            active = set()
            for pid in pids:
                try:
                    active.add(Path(f'/proc/{pid}/comm').read_text().strip())
                except FileNotFoundError:
                    pass
            routing = active & set(DAEMONS.values())
            if routing != {DAEMONS[args.protocolo]} or 'zebra' not in active:
                raise RuntimeError(f'{name}: processos inesperados: {sorted(active)}')
            configdir = Path('/etc/frr') / name
            vty = configdir / 'vtysh.conf'
            if not vty.exists():
                vty.touch(mode=0o644)
            destination = output / 'configuracoes' / name
            destination.mkdir(parents=True)
            for filename in ('zebra.conf', f'{DAEMONS[args.protocolo]}.conf'):
                src = configdir / filename
                if src.exists():
                    shutil.copy2(src, destination / filename)
        run(ns('lab-h1', 'ping', '-c', '3', '-W', '2', '192.168.5.10'))
        (output / 'ambiente.txt').write_text(run(['uname', '-a']) + run(['free', '-h']) +
            run(['/usr/lib/frr/zebra', '--version']) + run(['tcpdump', '--version']))
        (output / 'traceroute.txt').write_text(run(ns('lab-h1', 'traceroute', '-n', '-w', '1', '-m', '10', '192.168.5.10')))
        summaries = []
        for repetition in range(1, 4):
            folder = output / f'amostra_{repetition}'
            folder.mkdir()
            print(f'Amostra {repetition}/3: verificando rotas e medindo RTT...', flush=True)
            tables = snapshot(folder, args.protocolo)
            ping = run(ns('lab-h1', 'ping', '-n', '-c', '30', '-i', '0.2', '-W', '2', '192.168.5.10'), allowed=(0, 1))
            (folder / 'ping.txt').write_text(ping)
            loss = re.search(r'([\d.]+)% packet loss', ping)
            rtt = re.search(r'= ([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+) ms', ping)
            if not loss or not rtt:
                raise RuntimeError('Ping sem resumo valido; veja ping.txt.')
            print('Medindo trafego de controle nos seis enlaces por 60 segundos...', flush=True)
            control = capture(folder, args.protocolo)
            after = folder / 'apos_captura'
            after.mkdir()
            snapshot(after, args.protocolo)
            summaries.append({'protocolo': args.protocolo, 'amostra': repetition,
                'duracao_controle_s': control[0]['duracao_s'],
                'pacotes_controle_6_enlaces': sum(r['pacotes'] for r in control),
                'bytes_quadros_controle_6_enlaces': sum(r['bytes_quadros'] for r in control),
                'bits_por_segundo_controle_6_enlaces': sum(r['bits_por_segundo'] for r in control),
                'rtt_medio_ms': float(rtt[2]), 'rtt_mdev_ms': float(rtt[4]),
                'perda_ping_percentual': float(loss[1]),
                'prefixos_ipv4_main_r1': tables[0]['prefixos_ipv4_tabela_main'],
                'redes_acesso_r1': tables[0]['redes_acesso_presentes']})
            write_csv(output / 'resumo.csv', summaries)
        (output / 'metodo.txt').write_text(__doc__ + '\n'
            'Tres amostras consecutivas de estado estavel, nao tres inicializacoes independentes.\n'
            'Ping: H1-H5, 30 requisicoes por amostra, intervalo 0.2 s, payload padrao 56 bytes.\n'
            'Controle: pacotes de protocolo, incluidos cabecalhos de enlace observados; BGP inclui ACK TCP.\n'
            'Cada transmissao em cada enlace e contada uma vez; um anuncio propagado em varios enlaces\n'
            'contribui em cada enlace. Bits/s = soma dos bytes observados * 8 / duracao da janela.\n'
            'Prefixos totais podem diferir conforme redes anunciadas; comparar tambem as cinco LANs.\n'
            'Nao interpretar este laboratorio pequeno como teste de escalabilidade de producao.\n')
        print('\nCOLETA CONCLUIDA\n' + (output / 'resumo.csv').read_text(), flush=True)
    finally:
        for path in [output, *output.rglob('*')]:
            os.chown(path, uid, gid)
    archive = Path(shutil.make_archive(str(output), 'zip', root_dir=parent, base_dir=output.name))
    os.chown(archive, uid, gid)
    print('ENVIE ESTE ZIP:', archive, flush=True)


if __name__ == '__main__':
    try:
        main()
    except (Exception, KeyboardInterrupt) as error:
        print('\nCOLETA INTERROMPIDA:', error, file=sys.stderr)
        sys.exit(1)
