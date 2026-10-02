# Приложение Nginx

На этом этапе подготовлены конфигурация, развертывание и проверки. Реальное
выполнение на Ubuntu VM и контейнерная проверка Nginx еще не проводились.

## Состав и доступ

Путь запроса внутри кластера:

```text
Тестовый Pod → nginx.demo.svc.cluster.local:80 → одна из двух реплик Nginx:8080
```

Для публикации подготовлен [Envoy Gateway](gateway.md). Сам Service Nginx остается
типа ClusterIP. Для ручной диагностики приложения на VM запустите:

```bash
sudo kubectl --kubeconfig=/etc/kubernetes/admin.conf -n demo \
  port-forward --address=127.0.0.1 service/nginx 8080:80
```

В другом терминале той же VM:

```bash
curl -i -H 'X-Request-ID: manual-demo-001' http://127.0.0.1:8080/
```

Ожидаются HTTP 200, тело `Hello World!` с завершающим переводом строки,
заголовки `X-Request-ID: manual-demo-001` и `X-App-Version: v1`.
Port-forward используется только для ручной диагностики: автоматический тест
обращается из Pod через DNS-имя Service, проверяя внутрикластерную маршрутизацию.

## Ресурсы Kubernetes

| Ресурс | Назначение |
|---|---|
| Namespace `demo` | Отдельная область приложения; Pod Security restricted |
| ConfigMap `nginx-config` | Полная конфигурация Nginx |
| Deployment `nginx` | Две реплики, rolling update с maxUnavailable 0 |
| Service `nginx` | ClusterIP, порт 80 → именованный порт http 8080 |

Запрос ресурсов одной реплики: 50m CPU и 32Mi RAM, предел: 250m CPU и 128Mi RAM.
При обновлении может временно работать третья реплика. Nginx работает от UID/GID
101, без Linux capabilities, без токена ServiceAccount, с seccomp RuntimeDefault.
Корневая файловая система только для чтения; `/tmp` — отдельный emptyDir до 64Mi.
Точка монтирования конфигурации использует subPath; checksum заставляет создать
новые Pod при изменениях, поэтому обновление ConfigMap не требует ручного reload.

Две реплики на одном узле не обеспечивают защиту от отказа этого узла.

## HTTP endpoints

| Путь | Ответ | Назначение |
|---|---|---|
| `/` | 200, `Hello World!` | Проверка приложения |
| `/healthz` | 200, `ok` | Startup и liveness probes |
| `/readyz` | 200, `ready` | Readiness probe |
| `/missing-<уникальный-ID>` | 404 | Демонстрация access/error-логов |

На `127.0.0.1:8081/stub_status` внутри Pod exporter читает метрики Nginx.
Через Service приложения эта точка не доступна. Prometheus собирает данные
exporter на отдельном порту 9113 каждой реплики.

## Формат логов

Access-логи идут в stdout в JSON. Пример структуры, не результат выполненного теста:

```json
{"time":"2026-10-01T18:00:00+00:00","request_id":"manual-demo-001","remote_addr":"10.244.0.5","method":"GET","uri":"/","status":200,"bytes_sent":13,"request_time":0.001,"host":"nginx.demo.svc.cluster.local","pod":"nginx-example"}
```

Error-логи идут в stderr в штатном текстовом формате Nginx. HTTP-запрос к
отсутствующему файлу создает error-запись с URI; уникальный ID в URI позволяет
сопоставить ее с JSON access-логом. Error-логи не объявляются JSON.

Входной `X-Request-ID` сохраняется, если содержит 1–64 символа из набора
`A–Z a–z 0–9 . _ : -`. Иначе Nginx создает собственный ID. Заголовок возвращается
клиенту и при HTTP 404. Поля access-лога экранируются через `escape=json`.
Идентификатор не является подтверждением личности клиента.

Просмотр логов на VM:

```bash
sudo kubectl --kubeconfig=/etc/kubernetes/admin.conf -n demo \
  logs -l app.kubernetes.io/name=nginx -c nginx --tail=100 --prefix=true
```

Fluentd добавлен отдельным этапом. Эти команды подтверждают формирование
контейнерных логов, но не их сбор и сохранение отдельной системой.

## Автоматическая проверка

```bash
make verify-app INVENTORY=inventory.local.ini EXTRA_ARGS='--ask-become-pass'
```

Проверяются:

1. Две готовые и обновленные реплики, наблюдение актуальной версии Deployment.
2. Закрепленный образ и адреса обеих реплик в EndpointSlice сервиса.
3. `nginx -t` в обоих контейнерах.
4. HTTP 200 и точное тело через Service из временного Pod.
5. HTTP 404, передача корректного request ID и замена некорректного ID.
6. Ответы health endpoints.
7. JSON access-записи 200/404 и соответствующая error-запись.

Успешный итог: `PASS application verification completed`. Отчет сохраняется на VM
в `/var/log/devops-foundation/verify-app.log`; при ошибке возвращается ненулевой код.

## Повторное развертывание

`make deploy` устанавливает приложение и Gateway, сравнивая ресурсы через server-side
diff и применяя изменения только при необходимости. Чужой namespace `demo` без метки владельца проекта
автоматизация не перезаписывает. Не запускайте несколько deploy одновременно.

После первого успешного развертывания сравните UID Pod до и после повторного
`make deploy`: при неизменной конфигурации они должны сохраниться. Затем измените
комментарий в `ansible/roles/application/files/nginx.conf`, повторите deploy и
проверьте новый checksum и обновление Pod. Завершите `make verify-app`.

## Метрики приложения

В каждый Pod добавлен `nginx-exporter`, читающий локальный `stub_status` на 8081.
Prometheus собирает порт 9113 каждой реплики отдельно. Service Nginx публикует
только HTTP-порт 80. Добавление exporter впервые вызывает rollout приложения.
Подробности: [инструкция мониторинга](monitoring.md).

Проверка доставки access/error-записей через Fluentd: [инструкция логирования](logging.md).
