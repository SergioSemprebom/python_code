"""
SIC - Sistema GED (Flet) + PostgreSQL
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Iterable, Sequence

import bcrypt
import flet as ft
import pandas as pd
import psycopg2
from dotenv import load_dotenv
from psycopg2.extras import RealDictCursor

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

SEARCH_COLUMNS = [
    "acervo",
    "tipo_documento",
    "caixa_dlm",
    "conteudo",
    "indice_01",
    "indice_02",
    "indice_03",
    "indice_04",
    "indice_05",
    "indice_06",
    "indice_07",
    "indice_08",
]

VISIBLE_COLUMNS = [
    "id",
    "cliente",
    "acervo",
    "tipo_documento",
    "caixa_dlm",
    "conteudo",
    "indice_01",
    "indice_02",
    "indice_03",
    "indice_04",
    "indice_05",
    "indice_06",
    "indice_07",
    "indice_08",
]

VISIBLE_COLUMNS_LABELS = [
    "ID",
    "Cliente",
    "Acervo",
    "Tipo Documento",
    "Caixa DLM",
    "Conteúdo",
    "Índice 01",
    "Índice 02",
    "Índice 03",
    "Índice 04",
    "Índice 05",
    "Índice 06",
    "Índice 07",
    "Índice 08",
]

CLIENT_OPTIONS = ["JUCESP", "IAMSPE", "SPPREV", "IMESP"]

QUERY_LIMIT = 100000


def _required_env(var_name: str) -> str:
    value = os.getenv(var_name)
    if not value:
        raise RuntimeError(f"Variável de ambiente obrigatória ausente: {var_name}")
    return value


def get_db_connection() -> psycopg2.extensions.connection | None:
    """
    Abre conexão com PostgreSQL e retorna o objeto de conexão.
    """
    try:
        conn = psycopg2.connect(
            host=_required_env("DB_HOST"),
            port=int(os.getenv("DB_PORT", "5432")),
            dbname=_required_env("DB_NAME"),
            user=_required_env("DB_USER"),
            password=_required_env("DB_PASSWORD"),
            connect_timeout=10,
            cursor_factory=RealDictCursor,
        )
        return conn
    except (psycopg2.Error, RuntimeError) as exc:
        logging.exception("Erro ao conectar ao PostgreSQL")
        print(f"❌ Erro ao conectar ao PostgreSQL: {exc}")
        return None


def validar_login(usuario: str, senha: str):
    """
    Valida login consultando a tabela 'usuarios' e comparando a senha
    com bcrypt.

    Retorno:
      (sucesso: bool, mensagem: str, usuario_logado: str|None, cliente: str|None)
    """
    connection = get_db_connection()
    if not connection:
        return False, "Erro ao conectar ao banco de dados.", None, None

    try:
        with connection.cursor() as cursor:
            sql = "SELECT usuario, senha, cliente FROM usuarios WHERE usuario = %s"
            cursor.execute(sql, (usuario,))
            row = cursor.fetchone()

            if not row:
                return False, "Usuário ou senha incorretos.", None, None

            hashed_senha_db = row.get("senha")
            if hashed_senha_db and bcrypt.checkpw(
                senha.encode("utf-8"),
                hashed_senha_db.encode("utf-8"),
            ):
                return True, "Login bem-sucedido!", row.get("usuario"), row.get("cliente")

            return False, "Usuário ou senha incorretos.", None, None

    except psycopg2.Error as exc:
        return False, f"Erro ao validar login: {exc}", None, None

    finally:
        connection.close()


def _create_user(
    cursor: psycopg2.extensions.cursor,
    usuario: str,
    senha: str,
    cliente: str,
    cpf: str,
) -> None:
    hashed_senha = bcrypt.hashpw(senha.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    hashed_cpf = bcrypt.hashpw(cpf.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    cursor.execute(
        """
        INSERT INTO usuarios (usuario, senha, cliente, cpf)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (usuario) DO NOTHING
        """,
        (usuario, hashed_senha, cliente, hashed_cpf),
    )


def create_initial_users() -> None:
    """
    Cria tabela 'usuarios' se não existir e cria usuários padrão.
    """
    connection = get_db_connection()
    if not connection:
        print("Erro ao conectar ao banco para criar usuários iniciais.")
        return

    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS usuarios (
                    id BIGSERIAL PRIMARY KEY,
                    usuario VARCHAR(255) NOT NULL UNIQUE,
                    senha VARCHAR(255) NOT NULL,
                    cliente VARCHAR(255),
                    cpf VARCHAR(255) UNIQUE
                )
                """
            )
            _create_user(cursor, "sysadmin", "123456", "sysadmin", "00000000000")
            _create_user(cursor, "admin", "12345", "JUCESP", "11111111111")
        connection.commit()
        print("Verificada/Criada tabela 'usuarios'.")

    except psycopg2.Error as exc:
        print(f"Erro ao criar tabela/usuários iniciais: {exc}")
        connection.rollback()

    finally:
        connection.close()


def _build_search_query(schema_table: str, filtro: str, cliente: str) -> tuple[str, tuple[str, ...]]:
    like_conditions = " OR ".join([f"{col} ILIKE %s" for col in SEARCH_COLUMNS])
    sql = f"""
        SELECT *
        FROM {schema_table}
        WHERE cliente ILIKE %s
        AND (
            {like_conditions}
        )
        ORDER BY id DESC
        LIMIT {QUERY_LIMIT}
    """
    params = (f"%{cliente}%",) + tuple([f"%{filtro}%"] * len(SEARCH_COLUMNS))
    return sql, params


def _set_status(
    msg_status: ft.Text,
    page: ft.Page,
    message: str,
    progress_bar: ft.ProgressBar | None = None,
    progress_value: float | None = None,
) -> None:
    msg_status.value = message
    if progress_bar is not None and progress_value is not None:
        progress_bar.value = progress_value
    page.update()


def buscar_dados_instrumentado(
    combo_cliente: ft.Dropdown,
    input_filtro: ft.TextField,
    data_table: ft.DataTable,
    page: ft.Page,
    msg_status: ft.Text,
    progress_bar: ft.ProgressBar,
    btn_export_xlsx: ft.ElevatedButton,
):
    """
    Faz a busca no PostgreSQL medindo tempos e atualizando UI.
    """
    cliente = combo_cliente.value
    valor_filtro = input_filtro.value.strip()

    if not cliente:
        _set_status(msg_status, page, "⚠ Selecione um cliente antes de buscar.")
        btn_export_xlsx.disabled = True
        return

    page.session.set("resultados_busca", None)
    btn_export_xlsx.disabled = True

    _set_status(msg_status, page, "Conectando ao banco de dados...", progress_bar, 0.05)

    start_time_total = time.time()

    start_time_db_connect = time.time()
    connection = get_db_connection()
    db_connect_duration = time.time() - start_time_db_connect

    if not connection:
        _set_status(
            msg_status,
            page,
            f"⚠ Erro ao conectar ao banco ({db_connect_duration:.2f}s).",
            progress_bar,
            0,
        )
        return

    _set_status(msg_status, page, "Conexão OK. Executando consulta...", progress_bar, 0.1)

    try:
        start_time_query_exec = time.time()
        with connection.cursor() as cursor:
            sql, params = _build_search_query("db_registro_doc.tb_doc", valor_filtro, cliente)
            cursor.execute(sql, params)
            query_exec_duration = time.time() - start_time_query_exec

            _set_status(
                msg_status,
                page,
                f"Consulta executada ({query_exec_duration:.2f}s). Buscando resultados...",
                progress_bar,
                0.6,
            )

            start_time_fetch = time.time()
            resultados = cursor.fetchall()
            fetch_duration = time.time() - start_time_fetch

            _set_status(
                msg_status,
                page,
                f"Resultados buscados ({fetch_duration:.2f}s). Exibindo na tabela...",
                progress_bar,
                0.8,
            )

        start_time_populate = time.time()
        data_table.rows.clear()

        if resultados:
            page.session.set("resultados_busca", resultados)

            for row in resultados:
                data_table.rows.append(
                    ft.DataRow(
                        cells=[ft.DataCell(ft.Text(str(row.get(col, "-")))) for col in VISIBLE_COLUMNS]
                    )
                )

            btn_export_xlsx.disabled = False
            populate_duration = time.time() - start_time_populate

            msg_status.value = (
                f"✅ {len(resultados)} registros encontrados. "
                f"Query: {query_exec_duration:.2f}s, "
                f"Busca: {fetch_duration:.2f}s, "
                f"Tabela: {populate_duration:.2f}s."
            )
        else:
            msg_status.value = "⚠ Nenhum registro encontrado."
            btn_export_xlsx.disabled = True

        progress_bar.value = 1.0
        total_duration = time.time() - start_time_total
        msg_status.value += f" Tempo total: {total_duration:.2f}s."
        page.update()

    except psycopg2.Error as exc:
        _set_status(msg_status, page, f"⚠ Erro ao buscar os dados: {exc}", progress_bar, 0)

    finally:
        connection.close()


def limpar_tela(
    combo_cliente: ft.Dropdown,
    input_filtro: ft.TextField,
    data_table: ft.DataTable,
    msg_status: ft.Text,
    page: ft.Page,
    btn_export_xlsx: ft.ElevatedButton,
):
    """
    Limpa inputs, tabela e estado da sessão.
    """
    combo_cliente.value = None
    input_filtro.value = ""
    data_table.rows.clear()
    msg_status.value = ""
    btn_export_xlsx.disabled = True
    page.session.set("resultados_busca", None)
    page.update()


def _options_from(values: Iterable[str]) -> list[ft.dropdown.Option]:
    return [ft.dropdown.Option(value) for value in values]


# ============================================================
# Telas (Flet)
# ============================================================

def exibir_tela_consulta(page: ft.Page, usuario: str, cliente: str):
    """
    Tela principal após login: consulta + exportação.
    """
    page.clean()

    def exportar_direto_para_downloads(e):
        resultados = page.session.get("resultados_busca")

        if not resultados:
            msg_status.value = "⚠ Sem dados para exportar."
            page.update()
            return

        try:
            downloads = Path.home() / "Downloads"
            downloads.mkdir(exist_ok=True)

            caminho_arquivo = downloads / "consulta_clientes.xlsx"

            df = pd.DataFrame(resultados)
            df.to_excel(caminho_arquivo, index=False)

            msg_status.value = f"✅ Arquivo salvo em: {caminho_arquivo}"
        except Exception as exc:  # noqa: BLE001 - UI feedback for export errors
            msg_status.value = f"⚠ Erro ao salvar arquivo: {exc}"

        page.update()

    titulo = ft.Text(f"Consultar Cliente ({usuario})", size=24, weight="bold")

    opcoes_dropdown = _options_from(CLIENT_OPTIONS if cliente == "sysadmin" else [cliente])

    combo_cliente = ft.Dropdown(
        label="Selecione o Cliente",
        options=opcoes_dropdown,
        width=200,
        border=ft.InputBorder.OUTLINE,
        border_width=0.5,
    )

    input_filtro = ft.TextField(
        label="Digite o valor da pesquisa",
        width=200,
        border=ft.InputBorder.OUTLINE,
        border_width=0.5,
    )

    msg_status = ft.Text("", size=16, weight="bold", color="blue")
    progress_bar = ft.ProgressBar(width=400, value=0)

    btn_export_xlsx = ft.ElevatedButton(
        "Exportar para Excel",
        icon=ft.icons.DOWNLOAD,
        disabled=True,
        on_click=exportar_direto_para_downloads,
    )

    data_table = ft.DataTable(
        columns=[ft.DataColumn(ft.Text(col)) for col in VISIBLE_COLUMNS_LABELS],
        rows=[],
    )

    btn_buscar = ft.ElevatedButton(
        "Buscar",
        on_click=lambda _: buscar_dados_instrumentado(
            combo_cliente,
            input_filtro,
            data_table,
            page,
            msg_status,
            progress_bar,
            btn_export_xlsx,
        ),
    )

    btn_limpar = ft.ElevatedButton(
        "Limpar",
        on_click=lambda _: limpar_tela(
            combo_cliente,
            input_filtro,
            data_table,
            msg_status,
            page,
            btn_export_xlsx,
        ),
    )

    def logout(e):
        page.clean()
        exibir_tela_login(page)

    btn_logout = ft.ElevatedButton("Logout", on_click=logout)

    page.add(
        ft.Column(
            [
                titulo,
                ft.Row(
                    [combo_cliente, input_filtro, btn_buscar, btn_export_xlsx, btn_limpar, btn_logout]
                ),
                msg_status,
                progress_bar,
                ft.Container(content=data_table, expand=True),
            ]
        )
    )
    page.update()


def exibir_tela_login(page: ft.Page):
    """
    Tela inicial de login.
    """

    def processar_login(e):
        usuario = input_usuario.value.strip()
        senha = input_senha.value.strip()

        sucesso, mensagem, usuario_logado, cliente = validar_login(usuario, senha)

        if sucesso:
            page.session.set("usuario", usuario_logado)
            page.session.set("cliente", cliente)
            exibir_tela_consulta(page, usuario_logado, cliente)
        else:
            msg_login.value = f"❌ {mensagem}"
            page.update()

    def navegar_para_registro(e):
        exibir_tela_registro(page)

    def navegar_para_reset_senha(e):
        exibir_tela_reset_senha(page)

    page.clean()

    titulo = ft.Text("Login", size=24, weight="bold")

    input_usuario = ft.TextField(
        label="Usuário",
        hint_text="Digite seu nome de usuário",
        width=250,
        text_align="center",
        border=ft.InputBorder.OUTLINE,
        border_width=0.5,
    )

    input_senha = ft.TextField(
        label="Senha",
        hint_text="Digite sua senha",
        password=True,
        width=250,
        text_align="center",
        border=ft.InputBorder.OUTLINE,
        border_width=0.5,
    )

    msg_login = ft.Text("", size=16, weight="bold", color="red")

    btn_login = ft.ElevatedButton("Entrar", on_click=processar_login)
    btn_registrar = ft.TextButton("Se Inscrever", on_click=navegar_para_registro)
    btn_esqueci_senha_login = ft.TextButton(
        "Esqueci minha senha?",
        on_click=navegar_para_reset_senha,
    )

    container = ft.Container(
        content=ft.Column(
            [
                titulo,
                input_usuario,
                input_senha,
                btn_login,
                msg_login,
                btn_registrar,
                btn_esqueci_senha_login,
            ],
            alignment="center",
            horizontal_alignment="center",
            spacing=15,
        ),
        alignment=ft.alignment.center,
        padding=50,
    )

    page.add(container)
    page.update()


# ============================================================
# Telas não implementadas aqui (mantidas como no seu original)
# ============================================================

def exibir_tela_registro(page: ft.Page):
    """
    Tela de registro (cadastro).
    """
    page.clean()
    page.add(ft.Text("Tela de Registro (placeholder)"))
    page.add(ft.ElevatedButton("Voltar", on_click=lambda e: exibir_tela_login(page)))
    page.update()


def exibir_tela_reset_senha(page: ft.Page):
    """
    Tela de reset de senha.
    """
    page.clean()
    page.add(ft.Text("Tela de Reset de Senha (placeholder)"))
    page.add(ft.ElevatedButton("Voltar", on_click=lambda e: exibir_tela_login(page)))
    page.update()


# ============================================================
# Função principal do Flet
# ============================================================

def main(page: ft.Page):
    """
    Inicializa app Flet:
      - configura UI
      - garante tabela/usuários
      - abre tela de login
    """
    page.title = "SIC - Sistema GED"
    page.theme_mode = ft.ThemeMode.DARK
    page.scroll = ft.ScrollMode.ADAPTIVE
    page.window_width = 1200
    page.window_height = 800

    create_initial_users()
    exibir_tela_login(page)


if __name__ == "__main__":
    ft.app(target=main)
