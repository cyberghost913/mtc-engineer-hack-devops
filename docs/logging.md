# Сбор access/error-логов через Fluentd

Подготовлен пятый этап: Fluentd читает журналы контейнеров Nginx и сохраняет
собранные записи в JSON Lines на диске единственной Ubuntu VM. Внешние сервисы,
Elasticsearch, Loki и дополнительные Fluentd-плагины не требуются.
Развертывание и реальная доставка на VM пока не проверены.

## Путь записи

```text
Запрос → Gateway NodePort → Nginx
                            ├─ stdout: JSON access-log
                            └─ stderr: текстовый error-log
                                      ↓ containerd / kubelet
                      /var/log/pods/demo_nginx-*/nginx/*.log*
                                      ↓ Fluentd DaemonSet
                      постоянные позиции чтения + файловый буфер
                                      ↓
                /var/lib/devops-foundation/fluentd/archive/*.jsonl
```

Fluentd 1.19.3, образ `v1.19.3-debian-2.2`, закреплен по digest в `versions.yml`.
Наличие linux/amd64 и linux/arm64 проверено в реестре; сведения находятся
в `logging-image-lock.json`. Это не подтверждение запуска на обеих архитектурах.

Собираются только контейнеры `nginx` в Pod `nginx-*` namespace `demo`.
Логи exporter, Envoy и самого Fluentd не попадают в этот поток, поэтому
рекурсивного сбора журнала сборщика нет. Выбор сделан по пути стандартных
журналов kubelet; конфигурация не требует доступа к Kubernetes API.

CRI-оболочка сохраняется в полях `cri_time`, `stream`, `cri_flag`, `message`.
Добавляются `namespace`, `pod`, `pod_uid`, `container`, `node`, `source_path`
и `collector`. JSON access-log дополнительно разбирается в объект `app`.
Текстовый error-log сохраняется в `message`, даже когда JSON-разбор невозможен.
Ошибочные CRI-строки сохраняются в `unmatched_line`. Поле `collected_at` — время
сбора; исходное время приложения остается в `app.time`, контейнера — в `cri_time`.

Длинные сообщения, разбитые containerd на CRI-фрагменты `P/F`, сохраняются
по фрагментам с исходными флагами; автоматическая склейка не реализована.
Нельзя считать отдельный `P`-фрагмент полным JSON access-событием.

## Установка

Общий `make deploy` теперь включает приложение, Gateway, мониторинг и логирование.
Для уже подготовленного четвертого этапа:

```bash
make deploy-logging INVENTORY=inventory.local.ini EXTRA_ARGS='--ask-become-pass'
make verify-logging INVENTORY=inventory.local.ini EXTRA_ARGS='--ask-become-pass'
```

С управляющей машины используйте `inventory.ini`. Если применяете `config.yml`,
передавайте его так же, как на предыдущих этапах.

Сценарий создает namespace `logging`, ConfigMap, DaemonSet, каталоги состояния
и systemd timer хранения архивов. Первый запуск требует 1 GiB свободного места
в `/var/lib`. Сборщик запрашивает 100m CPU / 128 MiB RAM, лимиты — 500m / 384 MiB.
Устанавливаемые systemd units: `devops-log-retention.service` и `.timer`.

Init-контейнер выполняет `fluentd --dry-run` тем же закрепленным образом.
Checksum конфигурации запускает rollout при изменениях. Повторный deploy с теми
же файлами не должен заменять Pod. Одновременно разрешен один сборщик на узле;
surge при обновлении отключен, чтобы два процесса не делили файл позиций.
Во время обновления сборщик временно недоступен, журналы остаются у kubelet.

`/etc/devops-foundation/logging-lock.json` фиксирует образ, узел, путь данных
и retention. Сценарий не присваивает чужие ресурсы, существующий каталог данных
без lock или чужие systemd units. Неявная смена версии/пути/узла отклоняется.

## Доступ и исключения для сборщика

Каталог `/var/log/pods` подключен с хоста только для чтения. Сборщик работает
с UID 0, чтобы читать журналы, принадлежащие root, но без privileged-контейнера,
Linux capabilities, hostNetwork, hostPID и Kubernetes ServiceAccount token.
Корневая файловая система контейнера доступна только для чтения.

Namespace `logging` имеет Pod Security профиль `privileged`, поскольку
hostPath запрещен профилями baseline/restricted. Исключение ограничено этим
namespace; сам контейнер сохраняет перечисленные ограничения. Mount дает
техническую возможность прочитать журналы других Pod на узле, хотя glob
конфигурации собирает только Nginx. Это профиль доверенного лабораторного стенда.

Сетевой прием логов не включен, Service для Fluentd не создается.
Monitor endpoint слушает только `127.0.0.1:24220` внутри Pod. Пробы проверяют
ответ endpoint и наличие плагинов чтения/вывода; это проверка процесса,
а не доказательство доставки конкретного события.

## Хранение и ограничения

| Каталог VM | Назначение |
|---|---|
| `.../fluentd/positions` | Позиции чтения по inode |
| `.../fluentd/buffer` | Постоянный буфер вывода, лимит 256 MiB |
| `.../fluentd/archive` | Готовые файлы `nginx.YYYYMMDDHHMM.jsonl`, время UTC |

Полный префикс — `/var/lib/devops-foundation`.
Каталоги имеют права 0700, записи — 0600. Состояние хранится вне Pod.
Проверка сохранности после замены Pod и перезагрузки VM еще предстоит.

Вывод сбрасывается примерно раз в 5 секунд при нормальной работе; это не SLA.
При ошибках записи включены повторные попытки и обратное давление после
заполнения буфера. Новый Pod продолжит с сохраненных позиций. Используются
`follow_inodes`, чтение с начала новых файлов и ожидание завершения ротации.
Собираются текущие и несжатые ротированные файлы; `.gz` исключены.

Почасовой timer удаляет только готовые архивы с ожидаемым именем, не изменявшиеся
более 72 часов. Допустима задержка удаления примерно до часа. Файлы позиций,
буфера, посторонние имена и symlink не удаляются. Проверка без удаления:

```bash
sudo python3 /etc/devops-foundation/cleanup_logs.py --dry-run
sudo systemctl status devops-log-retention.timer
```

Retention по времени не является дисковой квотой. При высоком потоке архивы
могут заполнить диск раньше 72 часов; контролируйте свободное место VM.
После повторной попытки записи возможны дубли. Аварийное завершение, переполнение
диска, удаление исходных журналов kubelet или длительный простой сборщика могут
привести к потере записей. Гарантия exactly-once не заявляется. Архив и буфер
на одном диске: потеря VM означает потерю обоих, резервной копии нет.
Для нескольких узлов потребуется отдельная схема доставки в общее хранилище.

## Проверка доставки

`make verify-logging` выполняет следующие действия:

1. Проверяет текущую ревизию DaemonSet, образ, узел и успешный init dry-run.
2. Находит фактический HTTP NodePort Gateway и отправляет два свежих запроса:
   успешный `/?logging_probe=<UUID>` и отсутствующий `/missing-<UUID>`.
3. Проверяет HTTP 200 с точным телом, HTTP 404, заголовки Gateway/приложения
   и фактические request ID, возвращенные Envoy.
4. Находит в готовых JSONL-файлах access-записи обоих запросов и error-запись
   именно от Pod, обслужившего 404. Сверяет URI, request ID, статус, stream,
   pod UID и источник. Старые записи или только stdout не проходят проверку.
5. Проверяет наличие постоянного файла позиций и включенный retention timer.

Ожидание доставки ограничено 180 секундами. Читаются ограниченные хвосты
16 последних файлов; при интенсивной нагрузке диагностируйте архив отдельно.
Проверка намеренно порождает одну ошибку 404 и соответствующие access/error-логи.

Отчет с найденными записями:

```bash
sudo cat /var/log/devops-foundation/verify-logging.log
sudo sh -c 'tail -n 5 /var/lib/devops-foundation/fluentd/archive/*.jsonl'
```

Отчет завершает строка `PASS logging verification completed...`.
Успех подтверждает наличие новых записей в назначении Fluentd. Логи `kubectl`
используются только для диагностики и не заменяют это доказательство.
Для большого архива выберите один конкретный JSONL-файл вместо wildcard.

Диагностика:

```bash
sudo kubectl --kubeconfig=/etc/kubernetes/admin.conf -n logging get pods,daemonsets
sudo kubectl --kubeconfig=/etc/kubernetes/admin.conf -n logging logs daemonset/fluentd -c check-config
sudo kubectl --kubeconfig=/etc/kubernetes/admin.conf -n logging logs daemonset/fluentd -c fluentd --tail=100
sudo journalctl -u devops-log-retention.service -n 50 --no-pager
sudo du -sh /var/lib/devops-foundation/fluentd
```

## Локальные проверки

`make lint test` проверяет Ansible/YAML, шаблоны, защиту архивов при очистке,
корреляцию событий и отклонение старых/неполных/чужих записей.
На Mac отдельно проверены Ruby-синтаксис health probe, синтаксическая структура
конфигурации парсером Fluentd 1.19.3 и регулярное выражение CRI. Это не полный
запуск Fluentd с плагинами: `fluentd --dry-run` и живой сбор пока не выполнялись.

## Официальные источники

- [Образы Fluentd](https://github.com/fluent/fluentd-docker-image)
- [Чтение и ротация файлов](https://docs.fluentd.org/input/tail)
- [Parser filter и сохранение исходной записи](https://docs.fluentd.org/filter/parser)
- [Вывод в файлы](https://docs.fluentd.org/output/file)
- [Файловые буферы и повторные попытки](https://docs.fluentd.org/configuration/buffer-section)
