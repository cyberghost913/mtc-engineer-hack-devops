#!/usr/bin/env python3
"""Build the four-page submission passport from the documented project state."""
import argparse
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.graphics.shapes import Drawing, Rect, String, Line, Polygon
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak

ROOT = Path(__file__).resolve().parents[1]
REPO = 'https://github.com/cyberghost913/mtc-engineer-hack-devops/tree/main'
INK = colors.HexColor('#142C3E')
MUTED = colors.HexColor('#53616D')
LIGHT = colors.HexColor('#EDF2F5')
BLACK = colors.black
WIDTH = A4[0] - 84


def fonts(directory=None):
    candidates = [Path(directory)] if directory else [
        Path('/usr/share/fonts/truetype/dejavu'), Path('/System/Library/Fonts/Supplemental')]
    for folder in candidates:
        for regular, bold in [('DejaVuSans.ttf', 'DejaVuSans-Bold.ttf'), ('Arial.ttf', 'Arial Bold.ttf')]:
            if (folder / regular).is_file() and (folder / bold).is_file():
                pdfmetrics.registerFont(TTFont('Body', str(folder / regular)))
                pdfmetrics.registerFont(TTFont('BodyBold', str(folder / bold)))
                pdfmetrics.registerFontFamily('Body', normal='Body', bold='BodyBold', italic='Body', boldItalic='BodyBold')
                return
    raise SystemExit('Install DejaVu Sans or pass --font-dir containing DejaVuSans.ttf and DejaVuSans-Bold.ttf')


def architecture():
    drawing = Drawing(WIDTH, 174)
    def box(x, y, w, h, lines):
        drawing.add(Rect(x, y, w, h, fillColor=LIGHT, strokeColor=colors.HexColor('#BDC9D2'), strokeWidth=0.6, rx=3))
        for i, text in enumerate(lines):
            drawing.add(String(x + w / 2, y + h / 2 + (len(lines) - 1) * 5 - i * 11 - 3,
                               text, fontName='Body', fontSize=8.4, textAnchor='middle', fillColor=INK))
    def arrow(x1, y1, x2, y2, dashed=False):
        drawing.add(Line(x1, y1, x2, y2, strokeColor=MUTED, strokeWidth=0.8,
                         strokeDashArray=[3, 2] if dashed else None))
        if x2 > x1:
            pts=[x2,y2,x2-4,y2+2.5,x2-4,y2-2.5]
        elif x2 < x1:
            pts=[x2,y2,x2+4,y2+2.5,x2+4,y2-2.5]
        elif y2 > y1:
            pts=[x2,y2,x2-2.5,y2-4,x2+2.5,y2-4]
        else:
            pts=[x2,y2,x2-2.5,y2+4,x2+2.5,y2+4]
        drawing.add(Polygon(pts, fillColor=MUTED, strokeColor=MUTED))
    box(0, 130, 70, 36, ['Пользователь'])
    box(94, 130, 126, 36, ['Envoy Gateway', 'NodePort + HTTPRoute'])
    box(244, 130, 100, 36, ['Service nginx'])
    box(368, 130, 143, 36, ['Nginx', '2 реплики'])
    for left,right in [(70,94),(220,244),(344,368)]:arrow(left,148,right,148)
    box(94, 61, 126, 36, ['Prometheus'])
    box(244, 61, 100, 36, ['API / Kubelet', 'kube-state-metrics'])
    box(368, 61, 143, 36, ['Fluentd'])
    arrow(244,79,220,79,True)
    arrow(157,97,157,130,True)
    drawing.add(Line(157,110,439,110,strokeColor=MUTED,strokeWidth=0.8,strokeDashArray=[3,2]))
    arrow(439,110,439,130,True)
    drawing.add(String(173,114,'метрики',fontName='Body',fontSize=7.5,fillColor=MUTED))
    arrow(492,130,492,97)
    drawing.add(String(385,100,'CRI stdout / stderr',fontName='Body',fontSize=7.5,fillColor=MUTED))
    box(94, 0, 126, 34, ['Локальный PV', 'история метрик'])
    box(368, 0, 143, 34, ['JSONL архив', 'диск Ubuntu VM'])
    arrow(157,61,157,34)
    arrow(439,61,439,34)
    return drawing


def build(output, font_dir=None):
    fonts(font_dir)
    styles = {
        'title': ParagraphStyle('Title', fontName='BodyBold', fontSize=22, leading=26, textColor=BLACK, spaceAfter=9),
        'h1': ParagraphStyle('Heading1', fontName='BodyBold', fontSize=17, leading=21, textColor=BLACK, spaceAfter=12),
        'h2': ParagraphStyle('Heading2', fontName='BodyBold', fontSize=11.2, leading=15, textColor=BLACK, spaceBefore=12, spaceAfter=5),
        'body': ParagraphStyle('Body', fontName='Body', fontSize=10, leading=14, spaceAfter=7, textColor=BLACK),
        'small': ParagraphStyle('Small', fontName='Body', fontSize=8.7, leading=12, spaceAfter=6, textColor=MUTED),
        'cell': ParagraphStyle('Cell', fontName='Body', fontSize=9.1, leading=12, textColor=BLACK),
        'head': ParagraphStyle('TableHeader', fontName='BodyBold', fontSize=9.1, leading=12, textColor=colors.white),
        'code': ParagraphStyle('Command', fontName='Body', fontSize=9.2, leading=13, spaceAfter=4, textColor=INK),
    }
    def p(text, style='body'):
        return Paragraph(text, styles[style])
    def table(headers, rows, widths):
        data = [[p(escape(h),'head') for h in headers]] + [[p(c,'cell') for c in row] for row in rows]
        t=Table(data,colWidths=widths,repeatRows=1,hAlign='LEFT')
        t.setStyle(TableStyle([
            ('BACKGROUND',(0,0),(-1,0),INK),('VALIGN',(0,0),(-1,-1),'MIDDLE'),
            ('GRID',(0,0),(-1,-1),0.45,colors.HexColor('#D9D9D9')),
            ('LEFTPADDING',(0,0),(-1,-1),8),('RIGHTPADDING',(0,0),(-1,-1),8),
            ('TOPPADDING',(0,0),(-1,-1),7),('BOTTOMPADDING',(0,0),(-1,-1),7),
            ('ROWBACKGROUNDS',(0,1),(-1,-1),[colors.white,colors.HexColor('#F4F6F8')]),
        ]))
        return t

    story=[p('Паспорт DevOps решения','title'),p('MTC ENGINEER HACK  |  Состояние на 3 октября 2026 года','small'),
        p('Решение автоматизирует одноузловой стенд: веб-приложение в Kubernetes, публикацию через Gateway API, '
          'мониторинг Prometheus и сбор логов Fluentd. Для воспроизведения нужна отдельная Ubuntu Server 24.04; коммерческие сервисы не требуются.'),
        p('<b>Статус проверки.</b> Решение развёрнуто и проверено на Ubuntu 24.04 (amd64 и arm64): полный запуск '
          'с нуля, повторный запуск, перезагрузка VM и внешний HTTP-запрос через Gateway. '
          'Все обязательные компоненты работают.'),
        p('Архитектура','h2'), architecture(), Spacer(1,8),
        p('Состав и версии','h2'),
        table(['Компонент','Реализация'],[
            ['Kubernetes','kubeadm 1.35.9; один control-plane узел с рабочими Pod'],
            ['Runtime и сеть','containerd.io 2.2.6; Calico 3.32.2, VXLAN, IPv4'],
            ['Приложение','nginx-unprivileged 1.30.5-alpine; две реплики; Hello World!'],
            ['Gateway API','Envoy Gateway 1.9.2; CRD 1.6.1; Envoy Proxy 1.39.1'],
            ['Мониторинг','Prometheus 3.13.3; kube-state-metrics 2.19.1; Nginx exporter 1.5.3'],
            ['Логирование','Fluentd 1.19.3; постоянный файловый буфер и JSONL архив'],
            ['Автоматизация','Ansible Core 2.19.3, Makefile, Python; manifests из репозитория'],
        ],[115,WIDTH-115]),Spacer(1,9),
        p('Prometheus устанавливается собственными YAML-манифестами через Ansible. Рекомендуемая VM: '
          '4 vCPU, 8 ГБ RAM, 40 ГБ диска, стабильный IPv4 и исходящий интернет. Метрики и логи хранятся на этой VM.','small'),
        p(f'Исходники и инструкция: <link href="{REPO}" color="#142C3E">github.com/cyberghost913/mtc-engineer-hack-devops</link>','small'),
        PageBreak(),p('Обязательные функции','h1'),
        p('Таблица описывает реализацию и способ приемки. Команды проверки выполняются на стенде '
          'после установки; их успешное выполнение на Ubuntu 24.04 подтверждено на amd64 и arm64.'),
        table(['Пункт кейса','Реализация и обоснование','Проверка экспертом'],[
            ['Kubernetes','kubeadm создает кластер на Ubuntu 24.04. Выбор соответствует приоритету кейса и не привязан к облачному провайдеру.',
             '<b>make bootstrap</b><br/><b>make verify</b><br/>API, Node, DNS и Pod/Service networking.'],
            ['Веб-приложение','Две реплики Nginx, Service, probes и лимиты. Простой ответ позволяет однозначно проверить HTTP; приложение пишет access/error-логи.',
             '<b>make verify-app</b><br/>HTTP 200, точное тело Hello World!, 404, health и логи.'],
            ['Gateway API','GatewayClass, Gateway, HTTPRoute и EnvoyProxy. Маршрут по Host demo.local и префиксу / направляет запрос в Service Nginx.',
             '<b>make verify-gateway</b><br/>Проверка статусов и NodePort. Выведенный curl повторить с другого компьютера.'],
            ['Prometheus','Семь jobs и восемь целей: API, Kubelet, CPU/RAM, состояние объектов, Nginx и Envoy. Собственный Prometheus без зависимости от внешнего сервиса.',
             '<b>make verify-monitoring</b><br/>Targets, свежие samples, запросы PromQL и вычисление правил.'],
            ['Fluentd','DaemonSet читает CRI stdout/stderr Nginx, сохраняет исходное сообщение и разобранный access-JSON. Назначение - локальный JSONL архив.',
             '<b>make verify-logging</b><br/>Новые запросы через Gateway, совпадение URI, request ID, pod UID и error-записи в архиве.'],
            ['Ubuntu 24.04','Целевая ОС проверяется preflight; настраиваются systemd, swap, cgroups, runtime и сеть. Подтверждено на amd64 и arm64.',
             '<b>make bootstrap</b> / <b>make verify-all</b><br/>Полный запуск, повторный deploy, перезагрузка и внешний HTTP.'],
            ['Автоматизация','Ansible и Makefile сводят работу к setup, doctor, bootstrap, deploy. Lock-файлы защищают от неявной миграции; diff предшествует apply.',
             'Повторить bootstrap/deploy; проверить отсутствие лишних rollout и сохранение данных. Затем перезагрузить VM.'],
            ['Документация','README содержит схему, версии, требования, запуск и проверки. Отдельные инструкции описывают приложение, Gateway, метрики, логи и ограничения.',
             'README.md<br/>docs/acceptance.md<br/>docs/local-validation.json'],
        ],[91,250,WIDTH-341]),Spacer(1,10),
        p('Для запуска внутри VM добавляйте <b>INVENTORY=inventory.local.ini</b> и при необходимости '
          '<b>EXTRA_ARGS=\'--ask-become-pass\'</b>. С управляющей машины используется настроенный inventory.ini.','small'),
        PageBreak(),p('Дополнительные возможности','h1'),
        p('Улучшения представлены конфигурациями и проверками в репозитории. Их эксплуатационные свойства '
          'нужно подтвердить на работающем стенде.'),
        table(['Возможность','Как реализована и зачем','Как проверить'],[
            ['Расширенные метрики','CPU/RAM, Ready, реплики, перезапуски, запросы и соединения Nginx, метрики Envoy. Семь правил обнаруживают отказы и исчезновение целей.',
             'verify-monitoring; запросы из docs/monitoring.md; контролируемый отказ.'],
            ['Проверяемая доставка логов','Уникальные HTTP 200/404 через Gateway сопоставляются с записями в назначении Fluentd. Это отделяет наличие stdout от факта сбора.',
             'verify-logging; отчет содержит access 200/404 и связанный stderr error.'],
            ['Сохранение данных','Prometheus использует локальный PV с Retain; Fluentd сохраняет позиции и буфер вне Pod. Для лаборатории не нужен внешний storage provisioner.',
             'Замена Pod и перезагрузка VM; запрос к истории метрик и проверка архива до/после.'],
            ['Ограничение последствий отказа','Nginx работает без root, с read-only rootfs, probes и ресурсными лимитами. У мониторинга отдельные учетные записи и ограниченные роли.',
             'Проверить manifests и runtime securityContext; удалить один Pod Nginx и дождаться восстановления.'],
            ['Контроль изменений','Версии и ключевые image digest закреплены; upstream-файлы проверяются по SHA-256. Checksums конфигураций управляют rollout.',
             'make lint test; повторный deploy; изменение конфигурации и наблюдение за rollout.'],
        ],[99,247,WIDTH-346]),
        p('Что подтверждено','h2'),
        p('<b>Ubuntu 24.04:</b> полный запуск, повторный bootstrap/deploy (Pod не пересоздаются), перезагрузка, '
          'внешний HTTP 200 через Gateway. Полный запуск — на amd64 и arm64; повторный запуск и перезагрузка — на arm64.'),
        p('<b>Локально:</b> <b>90 тестов</b>, синтаксис <b>11 Ansible playbooks</b>, YAML и контрольные суммы vendor-файлов. '
          'Promtool 3.13.3 проверил конфигурацию, семь правил и три сценария. Digest основных прикладных образов '
          'проверены в реестрах.'),
        p('Короткий путь воспроизведения','h2'),
        p('make setup<br/>make doctor<br/>make bootstrap<br/>make deploy<br/>make verify-all','code'),
        p('Параметры inventory и sudo указаны в README. Общий deploy устанавливает приложение, Gateway, '
          'мониторинг и Fluentd, затем выполняет проверки каждого этапа. CI/CD, TLS, Grafana и доставка '
          'уведомлений Alertmanager не заявлены.','small'),
        PageBreak(),p('Ревью и развитие решения','h1'),
        p('Главная особенность','h2'),
        p('Проверки связывают инфраструктуру с наблюдаемым результатом: HTTP-ответом, свежими метриками '
          'и конкретной записью в архиве логов. Установка, проверка и повторное применение описаны в одном '
          'репозитории и не требуют доступа к инфраструктуре участника.'),
        p('Самое существенное техническое решение','h2'),
        p('Главный компромисс - способ хранения логов: полный поисковый стек дает удобный поиск, но увеличивает '
          'ресурсы и сложность воспроизведения. Выбран Fluentd с локальным JSONL архивом, поскольку кейс допускает '
          'такую точку назначения, а проверка подтверждает доставку access/error-записей. Цена решения - отсутствие '
          'единого поиска и отказоустойчивости при потере VM.'),
        p('Возможное масштабирование','h2'),
        p('<b>Несколько узлов и HA.</b> Добавить control-plane и worker-узлы, балансировщик API и резервное '
          'копирование etcd; нужны дополнительные VM и отдельные приемочные испытания.'),
        p('<b>Общее хранение логов.</b> Передавать данные Fluentd в OpenSearch или другое открытое хранилище; '
          'потребуются диски, RAM, контроль доступа и политика резервного копирования.'),
        p('<b>Телеком-сценарии.</b> Разделить сервисы по hostname, добавить метрики задержек и ошибок по маршрутам, '
          'SLO и синтетические проверки из разных сегментов сети. Нужны генератор нагрузки и согласованные пороги.'),
        p('<b>Защита и эксплуатация.</b> Добавить TLS, NetworkPolicy, доставку уведомлений и CI для проверок '
          'конфигураций. Потребуются домен/сертификаты, выбранный канал уведомлений и секреты вне Git.'),
        p('Ограничения текущего профиля','h2'),
        p('Один узел, IPv4, исходящий интернет; нет HA и резервных копий. История Prometheus ограничена '
          '3 днями / 2 GB блоков, архивы Fluentd - 72 часами; эти настройки не являются дисковой квотой. '
          'Fluentd читает hostPath с UID 0 в отдельном namespace; длинные CRI P/F-фрагменты не склеиваются, '
          'при сбоях возможны дубли и потери.'),
        p('Готовность','h2'),
        p('Обязательная часть кейса выполнена и подтверждена на Ubuntu 24.04. Не заявлены и не требуются для '
          'обязательной части: HA/несколько узлов, TLS, Grafana, доставка уведомлений Alertmanager и CI/CD.'),
        p('Полный перечень испытаний: docs/acceptance.md. Результаты проверок: docs/local-validation.json. '
          'Формат сдачи: docs/submission.md.','small'),
    ]

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont('Body',8)
        canvas.setFillColor(MUTED)
        canvas.drawString(42,25,'MTC ENGINEER HACK  |  DevOps')
        canvas.drawRightString(A4[0]-42,25,f'{doc.page} / 4')
        canvas.restoreState()

    output.parent.mkdir(parents=True,exist_ok=True)
    doc=SimpleDocTemplate(str(output),pagesize=A4,rightMargin=42,leftMargin=42,topMargin=35,bottomMargin=43,
                          title='Паспорт DevOps решения MTC ENGINEER HACK',author='cyberghost913')
    doc.build(story,onFirstPage=footer,onLaterPages=footer)
    from pypdf import PdfReader
    pages=len(PdfReader(output).pages)
    if pages != 4 or output.stat().st_size > 15_000_000:
        raise SystemExit(f'Passport format check failed: {pages} pages, {output.stat().st_size} bytes')
    print(f'Passport: {pages} pages, {output.stat().st_size} bytes')


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=ROOT/'docs/Паспорт.pdf')
    parser.add_argument('--font-dir')
    args=parser.parse_args()
    build(args.output,args.font_dir)
