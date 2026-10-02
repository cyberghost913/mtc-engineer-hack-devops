# Prometheus и метрики стенда

Подготовлен четвертый этап: Prometheus, kube-state-metrics и Nginx exporter.
Конфигурация предназначена для существующего одноузлового стенда с Gateway.
Развертывание и сбор реальных данных на Ubuntu VM пока не проверены.

## Что собирается

| Job | Источник | Данные |
|---|---|---|
| `prometheus` | Сам Prometheus | Состояние сборщика и базы метрик |
| `kubernetes-apiserver` | Kubernetes API, HTTPS `/metrics` | Метрики API |
| `kubelet` | Kubelet через API proxy, `/metrics` | Метрики агента узла |
| `kubernetes-resources` | Kubelet через API proxy, `/metrics/resource` | CPU и память узла, Pod и контейнеров |
| `kube-state-metrics` | API объектов nodes, pods, deployments | Ready, реплики, перезапуски |
| `nginx` | Exporter в каждом из двух Pod | Соединения, запросы, доступность Nginx |
| `envoy` | Envoy Proxy, порт 19001, `/stats/prometheus` | Метрики прокси Gateway |

Сбор и вычисление правил — каждые 15 секунд. Для штатного стенда ожидаются
8 целей: по одной для каждого job, кроме Nginx, у которого две цели.
Discovery использует Kubernetes labels и имена портов; IP Pod не фиксируются.
Цели Nginx и Envoy не фильтруются по Ready, чтобы отказ оставался видимым.

Nginx exporter читает `127.0.0.1:8081/stub_status` внутри своего Pod и отдает
метрики на 9113. Service приложения продолжает публиковать только HTTP-порт 80.
`nginx_up` показывает связь exporter с Nginx, а `up` — связь Prometheus с exporter.
Счетчик запросов Nginx включает служебные обращения и проверки здоровья; это
не точный счетчик пользовательского трафика. `stub_status` не дает разбиение
HTTP-кодов или задержки запросов: для анализа трафика используйте метрики Envoy
и логи приложения.

Пока не устанавливаются Grafana, Alertmanager, node_exporter и metrics-server.
Метрики дисков/сетевых устройств ОС и отдельных etcd, scheduler, controller-manager
в этот профиль не включены. Prometheus не обеспечивает работу `kubectl top`.

## Установка на будущую VM

Для чистого стенда общий путь остается прежним: `make bootstrap`, затем
`make deploy`. Deploy последовательно проверит приложение, Gateway и мониторинг.
Для уже установленного третьего этапа:

```bash
make deploy-monitoring INVENTORY=inventory.local.ini EXTRA_ARGS='--ask-become-pass'
make verify-monitoring INVENTORY=inventory.local.ini EXTRA_ARGS='--ask-become-pass'
```

При запуске с управляющей машины используйте обычный `inventory.ini`.
Передавайте свой `-e @config.yml`, если он используется на других этапах.
Первое добавление exporter обновит оба Pod Nginx. Последующие запуски с теми же
файлами не должны вызывать rollout. Версии образов находятся в `versions.yml`,
результаты проверки реестров — в `monitoring-image-lock.json`.

Мониторинг запрашивает примерно 250m CPU и 576 MiB RAM, плюс два exporter по
10m CPU и 16 MiB RAM. Лимиты выше; рекомендуемая VM для всего стенда — 4 vCPU,
8 ГБ RAM. Первый запуск проверяет наличие 5 GiB свободного места в `/var/lib`.

## История метрик и повторный запуск

Prometheus использует локальный PV `devops-prometheus` и PVC
`monitoring/prometheus-data`. Каталог VM — `/var/lib/devops-foundation/prometheus`,
владелец 65534:65534. Каталог создается Ansible; динамический provisioner не нужен.
PV привязан к фактическому hostname label единственного узла, политика — `Retain`.
Deployment использует `Recreate`, чтобы исключить двух писателей в одну базу.
Обновление конфигурации ненадолго прерывает сбор.

История ограничена 3 днями и 2 GB блоков TSDB, срабатывает более раннее ограничение.
Заявленные 5 GiB PV не являются файловой квотой: WAL и активный блок требуют
дополнительного места. Нужно контролировать свободное место диска VM.
История рассчитана на сохранение при замене Pod и перезагрузке VM; это еще
нужно подтвердить испытанием. Потеря VM означает потерю истории: резервной копии нет.

Версии и путь данных фиксируются в `/etc/devops-foundation/monitoring-lock.json`.
Сценарий отклоняет неявную миграцию, чужие объекты с совпадающими именами,
символические ссылки вместо каталога и существующие данные без installation lock.
При частично прерванной установке сохраненный lock позволяет повторить запуск.
Обычный deploy не удаляет PVC, не очищает TSDB и не выполняет принудительный apply.
После ручного удаления PVC том может остаться `Released`; восстановление
привязки требует отдельного решения администратора, сценарий не стирает данные.

Изменения конфигурации и правил меняют checksum в Pod template.
Перед стартом Prometheus init-контейнер того же закрепленного образа выполняет
`promtool check config`, включая проверку файлов правил.

## Доступ к интерфейсу

Prometheus — внутренний ClusterIP Service. Для просмотра на самой VM:

```bash
sudo kubectl --kubeconfig=/etc/kubernetes/admin.conf -n monitoring \
  port-forward --address=127.0.0.1 service/prometheus 9090:9090
```

Оставьте эту команду работающей. С ноутбука создайте SSH-туннель, подставив
реальные имя пользователя и адрес VM:

```bash
ssh -N -L 9090:127.0.0.1:9090 ubuntu@VM_IP
```

Откройте `http://127.0.0.1:9090`. Targets показывает состояние сбора,
Alerts — правила, Query — значения и графики PromQL.
Интерфейс не опубликован через Gateway и не имеет отдельной аутентификации.
ClusterIP доступен другим Pod: изоляция межсервисного трафика NetworkPolicy пока
не настроена. Доступ к кластеру и его рабочим нагрузкам должен быть доверенным.

У Prometheus отдельный ServiceAccount. Ему разрешены discovery Pod/узлов,
чтение `/metrics` и `get nodes/proxy` для Kubelet. Проверка TLS API не отключается.
`nodes/proxy` дает более широкий доступ к Kubelet, чем только метрики, поэтому
это профиль доверенного лабораторного кластера. У kube-state-metrics отдельная
роль на list/watch nodes, pods, deployments; доступа к Secrets нет.

## Запросы для демонстрации

```promql
up
nginx_up{job="nginx"}
sum(rate(nginx_http_requests_total{job="nginx"}[2m]))
sum by (pod) (rate(container_cpu_usage_seconds_total{job="kubernetes-resources",namespace="demo",container="nginx"}[2m]))
container_memory_working_set_bytes{job="kubernetes-resources",namespace="demo",container="nginx"}
kube_deployment_status_replicas_available{namespace="demo",deployment="nginx"}
kube_pod_container_status_restarts_total{namespace="demo"}
envoy_server_live{job="envoy"}
```

Для `rate` сначала дождитесь нескольких сборов. Отправьте запросы через Gateway
по команде из `make verify-gateway`, затем посмотрите изменение счетчиков.

## Проверка и предупреждения

`verify-monitoring` обращается к API Prometheus через авторизованный Kubernetes
Service proxy. Он проверяет текущие Deployment и образы, PV/PVC и UID привязки,
все 8 свежих успешных целей, обе текущие реплики Nginx, реальные значения
метрик и успешное вычисление всех 7 правил. Свежесть исходных samples проверяется
через `timestamp(metric)`, отдельно от времени выполнения PromQL-запроса.
Отчет: `/var/log/devops-foundation/verify-monitoring.log`.
Успех не доказывает сохранение истории после рестарта или внешнюю доставку alert.

Правила покрывают неуспешный scrape, исчезнувший job, потерю метрик одной
реплики Nginx, ошибку чтения stub_status, неготовый узел, нехватку реплик
и повторные перезапуски контейнера. Hold period — 1–2 минуты.
Правила видны в Prometheus; отправка сообщений не настроена. При остановке самого
Prometheus предупреждения не вычисляются — внешний контроль отсутствует.

Локальные проверки без VM:

```bash
make lint test
# Нужен официальный promtool 3.13.3 для вашей ОС:
make lint-prometheus PROMTOOL=/path/to/promtool
```

Последняя команда проверяет синтаксис конфигурации, правила и три сценария:
здоровый стенд, отказы компонентов, исчезновение целей discovery. Для runtime
проверки путей и файлов остается init-контейнер на VM.

Диагностика на VM:

```bash
sudo kubectl --kubeconfig=/etc/kubernetes/admin.conf -n monitoring get pods,pvc
sudo kubectl --kubeconfig=/etc/kubernetes/admin.conf -n monitoring logs deployment/prometheus -c check-config
sudo kubectl --kubeconfig=/etc/kubernetes/admin.conf -n monitoring logs deployment/prometheus -c prometheus --tail=100
sudo kubectl --kubeconfig=/etc/kubernetes/admin.conf -n monitoring logs deployment/kube-state-metrics --tail=100
sudo kubectl --kubeconfig=/etc/kubernetes/admin.conf -n demo logs deployment/nginx -c nginx-exporter --tail=100
```

## Официальные источники

- [Prometheus: версии и контрольные суммы](https://prometheus.io/download/)
- [Kubernetes discovery и авторизация Prometheus](https://github.com/prometheus/prometheus/blob/v3.13.3/documentation/examples/prometheus-kubernetes.yml)
- [Хранение Prometheus](https://prometheus.io/docs/prometheus/latest/storage/)
- [kube-state-metrics: совместимость с Kubernetes](https://github.com/kubernetes/kube-state-metrics/blob/v2.19.1/README.md#compatibility-matrix)
- [Nginx exporter](https://github.com/nginx/nginx-prometheus-exporter)
- [Метрики Envoy Proxy](https://gateway.envoyproxy.io/docs/tasks/observability/proxy-metric/)
