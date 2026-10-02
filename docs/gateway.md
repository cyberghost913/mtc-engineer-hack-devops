# Доступ к Nginx через Gateway API

Подготовлены развертывание Envoy Gateway, маршрут HTTP и автоматические проверки.
Работа в Kubernetes пока не подтверждена: Ubuntu VM еще не предоставлена.

## Архитектура

```text
Клиент с Host: demo.local
  → IP узла Ubuntu : назначенный NodePort
  → Envoy Proxy
  → HTTPRoute nginx
  → Service nginx в namespace demo, порт 80
  → Nginx, порт 8080
```

Контроллер Envoy Gateway и управляемый Envoy Proxy работают в namespace
`envoy-gateway-system`. `Gateway`, `HTTPRoute` и `EnvoyProxy` находятся в `demo`.
`GatewayClass` — ресурс всего кластера. У Service приложения сохраняется тип
ClusterIP; наружу публикуется Service прокси.

## Версии и установка

Envoy Gateway 1.9.2, Gateway API 1.6.1 и Envoy Proxy distroless-v1.39.1.
Контроллер, shutdown manager и прокси закреплены digest в `versions.yml`.
Версия и digest прокси взяты из стандартной настройки исходников Gateway 1.9.2.
Контроллер проверен в реестре для amd64/arm64; дополнительная проверка реестра
для прокси завершилась сетевым таймаутом, скачивание и запуск еще не подтверждены.

Используется официальный `install.yaml`, сохраненный в `vendor/` с SHA-256.
Helm на VM не требуется. Bundle включает experimental CRD Gateway API; наши
GatewayClass, Gateway и HTTPRoute используют стабильный API `v1`, EnvoyProxy —
API расширения `gateway.envoyproxy.io/v1alpha1`.

Установочные файлы формируются и применяются последовательно:

1. Namespace контроллера.
2. CRD и политики проверки; ожидание Established.
3. ServiceAccount, RBAC, конфигурация, Deployment и webhook.
4. Job генерации внутренних сертификатов; ожидание Complete.
5. Ожидание готовности контроллера.
6. EnvoyProxy, GatewayClass, Gateway и HTTPRoute.
7. Проверка запроса через NodePort.

Завершенная certgen Job сохраняется: ее исходный TTL удален в производном
манифесте. Повторная установка не должна повторно запускать генерацию сертификатов.
Секреты создаются внутри кластера и не сохраняются в репозитории.

## Команды

После `make bootstrap` полный текущий набор устанавливается так:

```bash
make deploy INVENTORY=inventory.local.ini EXTRA_ARGS='--ask-become-pass'
```

Если Nginx уже установлен, выполните только Gateway:

```bash
make deploy-gateway INVENTORY=inventory.local.ini EXTRA_ARGS='--ask-become-pass'
make verify-gateway INVENTORY=inventory.local.ini EXTRA_ARGS='--ask-become-pass'
```

При SSH-запуске используйте свой `inventory.ini` вместо `inventory.local.ini`.
Если sudo не требует пароль, уберите `EXTRA_ARGS`. Для своих настроек передавайте
`EXTRA_ARGS='--ask-become-pass -e @config.yml'`. Параметр `gateway_hostname`
по умолчанию равен `demo.local`; DNS или изменение `/etc/hosts` для curl не нужны.

NodePort назначает Kubernetes. Автоматизация получает его по меткам владельца
Gateway, а не по случайно сгенерированному имени Service, и выводит команду:

```bash
curl --fail-with-body -i -H 'Host: demo.local' http://IP_УЗЛА:NODEPORT/
```

Замените адрес и порт значениями из вывода. Они также сохранены на VM:

```bash
sudo cat /etc/devops-foundation/gateway-endpoint.json
```

Ожидаются HTTP 200, тело `Hello World!` с переводом строки и заголовки
`X-Gateway: envoy-gateway`, `X-App-Version: v1`, `X-Request-ID`.
`X-Gateway` добавляется фильтром HTTPRoute и позволяет отличить ответ через Gateway.

Для проверки с отдельного компьютера ему нужен маршрут к IP VM и разрешение
на фактически назначенный TCP NodePort в сетевом экране VM/провайдера.
Автоматизация эти внешние правила не меняет. Диапазон kubeadm по умолчанию
30000–32767; достаточно разрешить назначенный порт для нужного адреса клиента.

## Что проверяет verify-gateway

- GatewayClass Accepted с актуальным observedGeneration.
- Gateway Accepted/Programmed, HTTP listener и присоединенный маршрут.
- HTTPRoute Accepted/ResolvedRefs именно для demo-gateway и listener http.
- Закрепленные образы контроллера и прокси.
- NodePort управляемого Service, найденного по меткам Gateway.
- HTTP 200, точное тело и заголовки при обращении на IP узла и NodePort.
- HTTP 404 для постороннего hostname без ответа Nginx.
- JSON access-запись Nginx с тем же URI и request ID из HTTP-ответа.

Проверка не использует port-forward и не следует HTTP-редиректам. Запрос содержит
уникальный query-параметр. Envoy может изменить входящий X-Request-ID, поэтому
сопоставление выполняется по ID фактического ответа и уникальному URI.

Отчет: `/var/log/devops-foundation/verify-gateway.log`. Проверка выполняется
на Ubuntu-узле и подтверждает путь NodePort → Envoy → Nginx из этой точки.
Доступность извне проверяется отдельной командой curl с клиентского компьютера.
Формирование access-лога проверяется через kubectl; Fluentd еще не подключен.

## Повторная установка и ограничения

Ресурсы обновляются через server-side diff/apply. Версии установки фиксируются
в `/etc/devops-foundation/gateway-lock.json`; автоматическое обновление версий
не выполняется. Чужие Gateway CRD или namespace на первой установке вызывают
остановку, чтобы не перезаписать существующий контроллер.

После повторного deploy должны сохраниться UID сертификатной Job и Secret,
UID неизмененных Pod и назначенный NodePort. Это еще предстоит проверить на VM.
Не удаляйте успешную certgen Job для повседневного повторного запуска. Восстановление
при потере Secret и ротация сертификатов требуют отдельного сценария; здесь их нет.

Один узел и одна реплика Envoy Proxy, HTTP без внешнего TLS, без облачного
балансировщика и публичного DNS. При удалении и пересоздании Service NodePort
может измениться. Автоматический rollback и обновление CRD между версиями не реализованы.

Диагностика на VM:

```bash
sudo kubectl --kubeconfig=/etc/kubernetes/admin.conf get gatewayclass devops-envoy -o yaml
sudo kubectl --kubeconfig=/etc/kubernetes/admin.conf -n demo get gateway,httproute -o yaml
sudo kubectl --kubeconfig=/etc/kubernetes/admin.conf -n envoy-gateway-system get pods,services,jobs
sudo kubectl --kubeconfig=/etc/kubernetes/admin.conf -n envoy-gateway-system logs deployment/envoy-gateway --tail=100
```

Официальные источники: [установка YAML](https://gateway.envoyproxy.io/docs/install/install-yaml/),
[матрица совместимости](https://gateway.envoyproxy.io/news/releases/matrix/),
[настройка EnvoyProxy](https://gateway.envoyproxy.io/docs/tasks/operations/customize-envoyproxy/).
