# Comparação de RIP, OSPF e BGP com FRRouting

**Luiza Birck Garcia de Oliveira**  
Professor: Cristiano Bonato Both

Implementei uma topologia com cinco roteadores e cinco hosts em uma VM Ubuntu. Comparei RIP, OSPF e BGP separadamente, usando FRRouting 8.4.4. No BGP, os roteadores pertencem a três Sistemas Autônomos.

## Arquivos

- `scripts/`: criação e operação do laboratório, coleta de métricas e teste de falha.
- `configuracoes/`: arquivos de configuração dos roteadores para RIP, OSPF e BGP.
- [Apresentação em PDF](Slipes_Apresentação.pdf): topologia, resultados e gráficos das métricas.
- [Vídeo completo](Video_Apresentacao.mp4): demonstração e análise dos resultados (9 min 28 s).

## Preparação

Use uma VM Ubuntu com acesso à Internet. Instale os programas necessários:

```bash
sudo apt update
sudo apt install -y frr iproute2 iputils-ping traceroute tcpdump python3
```

Baixe o repositório pelo botão **Code → Download ZIP**, extraia-o e abra o terminal na pasta que contém este README e `scripts/`.

## Executar o laboratório

Em uma VM nova ou após reiniciá-la, crie os namespaces e inicie um protocolo. Não execute `criar` se os namespaces `lab-r1` a `lab-r5` e `lab-h1` a `lab-h5` já existirem.

```bash
sudo python3 scripts/laboratorio.py criar
sudo python3 scripts/laboratorio.py iniciar rip
sudo python3 scripts/laboratorio.py status
sudo ip netns exec lab-h1 ping -c 5 -W 2 192.168.5.10
```

Para trocar de protocolo, use `iniciar` com `ospf` ou `bgp`. O script encerra o protocolo anterior dentro dos namespaces do laboratório. Aguarde as rotas se estabilizarem e confira a conectividade com o ping acima.

```bash
sudo python3 scripts/laboratorio.py iniciar ospf
sudo ip netns exec lab-r1 vtysh -N lab-r1 -c "show ip ospf neighbor"
```

Para BGP, confira as sessões e rotas:

```bash
sudo python3 scripts/laboratorio.py iniciar bgp
sudo ip netns exec lab-r1 vtysh -N lab-r1 -c "show bgp ipv4 unicast summary"
sudo ip netns exec lab-r1 vtysh -N lab-r1 -c "show bgp ipv4 unicast"
```

## Coletar métricas e testar falha

Após iniciar e estabilizar cada protocolo, rode os scripts correspondentes. Cada coleta leva cerca de quatro minutos; o teste de falha leva pouco mais de quatro minutos e religa o enlace ao terminar.

```bash
sudo python3 scripts/coletar_metricas.py rip
sudo python3 scripts/testar_falha_v2.py rip
```

Repita os dois comandos substituindo `rip` por `ospf` e depois por `bgp`. Os novos resultados são salvos em `~/trabalho-redes/resultados/`. Os resultados e gráficos dos experimentos apresentados estão no PDF.

## Topologia e encerramento

Cada host usa a rede `192.168.n.0/24`. Os enlaces entre roteadores são R1–R2, R1–R3, R2–R4, R3–R4, R4–R5 e R2–R5. No BGP, R1 e R2 estão no AS65001, R3 e R4 no AS65002, e R5 no AS65003. O OSPF usa a área 0.

Para parar os protocolos:

```bash
sudo python3 scripts/laboratorio.py parar
```

Para remover os dez namespaces do laboratório:

```bash
sudo python3 scripts/laboratorio.py remover
```