# Внешние исходные файлы

`calico-v3.32.2.yaml` — неизмененный манифест проекта Calico, лицензия Apache 2.0
в `calico-LICENSE`. Репозиторий: https://github.com/projectcalico/calico.
При установке измененная копия генерируется отдельно скриптом `render_calico.py`.

`docker.asc` и `kubernetes-release.key` — публичные ключи подписания официальных
APT-репозиториев. Они не являются приватными ключами или учетными данными.

В `sources.json` записаны URL и SHA-256 файлов, полученных 1 октября 2026 года.
Проверка: `python3 scripts/check_vendor.py` из корня проекта.

`envoy-gateway-v1.9.2.yaml` — неизмененный официальный установочный bundle
Envoy Gateway. Лицензия Apache 2.0 находится в `envoy-gateway-LICENSE`.
Скрипт `prepare_gateway.py` создает отдельные производные файлы: namespace,
CRD, controller и certgen. Изменения в производных файлах: digest образов,
метки управления, checksum конфигурации и удаление TTL завершенной certgen Job.
Оригинальный bundle не редактируется. Его URL и SHA-256 также включены в `sources.json`.
