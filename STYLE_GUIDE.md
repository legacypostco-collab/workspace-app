# Библиотеки и код-стайл

## Посмотреть библиотеки

```bash
cd "/Users/konastantinverveyn/Documents/Проект/django_marketplace"
source .venv/bin/activate
make libs
```

## Установить зависимости

```bash
make install
```

## Установить инструменты код-стайла

```bash
make install-dev
```

## Навести порядок в коде

```bash
make format
```

Что делает:
- сортирует и чинит импорты/часть проблем (`ruff --fix`)
- форматирует весь код единообразно (`black`)

## Проверить стиль без изменений

```bash
make check-style
```
