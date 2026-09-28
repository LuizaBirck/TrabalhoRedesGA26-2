#!/usr/bin/env python3
"""Gerencia exclusivamente lab-r1..r5 e lab-h1..h5. Ubuntu/FRR 8.4.
Script de reprodução organizado após os ensaios; validar na VM antes da demonstração.
"""
import argparse, json, os, pwd, grp, shutil, signal, subprocess, time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
LINKS=['12','13','24','34','45','25']
DAEMONS={'rip':'ripd','ospf':'ospfd','bgp':'bgpd'}
NAMES=[f'lab-{k}{n}' for n in range(1,6) for k in ['r','h']]

def run(*args):
    return subprocess.check_output(args,text=True)

def existing():return {x.split()[0] for x in run('ip','netns','list').splitlines()}

def processes(name):
    out=[]
    for pid in run('ip','netns','pids',name).split():
        try:out.append((int(pid),Path(f'/proc/{pid}/comm').read_text().strip()))
        except FileNotFoundError:pass
    return out

def stop_protocols(zebra=False):
    wanted=set(DAEMONS.values())|({'zebra'} if zebra else set())
    for name in NAMES:
        if name not in existing() or not name.startswith('lab-r'):continue
        for pid,comm in processes(name):
            if comm in wanted:
                try:os.kill(pid,signal.SIGTERM)
                except ProcessLookupError:pass
    for _ in range(50):
        alive=[(n,p) for n in NAMES if n in existing() and n.startswith('lab-r') for p,c in processes(n) if c in wanted]
        if not alive:return
        time.sleep(.1)
    raise RuntimeError('Processos não terminaram; interrompido sem forçar encerramento.')

def create():
    overlap=existing()&set(NAMES)
    if overlap:raise RuntimeError('Namespaces já existem: '+', '.join(sorted(overlap))+'. Use status; não recrie o laboratório ativo.')
    for n in range(1,6):
        r,h=f'lab-r{n}',f'lab-h{n}'
        for name in [r,h]:
            run('ip','netns','add',name);run('ip','-n',name,'link','set','lo','up')
        run('ip','netns','exec',r,'sysctl','-qw','net.ipv4.ip_forward=1')
        run('ip','link','add','lan0','netns',r,'type','veth','peer','name','eth0','netns',h)
        for ns,dev,addr in [(r,'lan0',f'192.168.{n}.1/24'),(h,'eth0',f'192.168.{n}.10/24')]:
            run('ip','-n',ns,'addr','add',addr,'dev',dev);run('ip','-n',ns,'link','set',dev,'up')
        run('ip','-n',h,'route','add','default','via',f'192.168.{n}.1')
    for pair in LINKS:
        a,b=pair
        run('ip','link','add',f'r{a}-r{b}','netns',f'lab-r{a}','type','veth','peer','name',f'r{b}-r{a}','netns',f'lab-r{b}')
        for n,m,host in [(a,b,1),(b,a,2)]:
            run('ip','-n',f'lab-r{n}','addr','add',f'10.0.{pair}.{host}/30','dev',f'r{n}-r{m}')
            run('ip','-n',f'lab-r{n}','link','set',f'r{n}-r{m}','up')
    print('Topologia criada.')

def start(proto):
    if not set(NAMES)<=existing():raise RuntimeError('Faltam namespaces. Execute criar em uma VM sem laboratório ou confira o estado.')
    daemon=DAEMONS[proto];uid=pwd.getpwnam('frr').pw_uid;gid=grp.getgrnam('frr').gr_gid
    # Valida todos os arquivos antes de interromper o protocolo anterior.
    for n in range(1,6):
        for file in ['zebra.conf',daemon+'.conf']:
            if not (ROOT/'configuracoes'/proto/f'lab-r{n}'/file).is_file():raise RuntimeError('Configuração ausente: '+file)
    stop_protocols()
    for pair in LINKS:
        a,b=pair
        for n,m in [(a,b),(b,a)]:run('ip','-n',f'lab-r{n}','link','set',f'r{n}-r{m}','up')
    try:
        for n in range(1,6):
            name=f'lab-r{n}';dest=Path('/etc/frr')/name
            for folder in [dest,Path('/run/frr')/name]:
                folder.mkdir(parents=True,exist_ok=True);os.chown(folder,uid,gid);folder.chmod(0o755)
            for file in ['zebra.conf',daemon+'.conf']:
                shutil.copyfile(ROOT/'configuracoes'/proto/name/file,dest/file);os.chown(dest/file,uid,gid);(dest/file).chmod(0o640)
            (dest/'vtysh.conf').touch(exist_ok=True)
            current={c for p,c in processes(name)}
            for d in (['zebra'] if 'zebra' not in current else [])+[daemon]:
                run('ip','netns','exec',name,f'/usr/lib/frr/{d}','-N',name,'-f',str(dest/(d+'.conf')),'-A','127.0.0.1','-d')
                if d=='zebra':time.sleep(.2)
        print(f'{proto.upper()} iniciado. Aguarde a formação das rotas e execute status antes de coletar.')
    except BaseException:
        stop_protocols();raise

def status():
    ex=existing()
    for n in range(1,6):
        name=f'lab-r{n}'
        if name not in ex:print(name,'ausente');continue
        print(name,', '.join(c for p,c in processes(name)))
        print(run('ip','-n',name,'-4','route','show'))

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('acao',choices=['criar','iniciar','status','parar','remover'])
    parser.add_argument('protocolo',nargs='?',choices=DAEMONS)
    args=parser.parse_args()
    if os.geteuid()!=0:parser.error('Execute com sudo python3.')
    if args.acao=='criar':create()
    elif args.acao=='iniciar':
        if not args.protocolo:parser.error('Informe rip, ospf ou bgp.')
        start(args.protocolo)
    elif args.acao=='status':status()
    elif args.acao=='parar':stop_protocols()
    elif args.acao=='remover':
        stop_protocols(zebra=True)
        if any(processes(n) for n in NAMES if n in existing()):raise RuntimeError('Há outros processos nos namespaces; feche pings/capturas antes de remover.')
        for n in NAMES:
            if n in existing():run('ip','netns','del',n)
        print('Namespaces do laboratório removidos. Resultados e configurações preservados.')
