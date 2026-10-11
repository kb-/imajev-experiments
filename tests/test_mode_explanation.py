"""The bottom Diagnostics feed explains the selected experiment before play."""
from app.config import Config
from app.core.session import SessionController
from app.games.tic_tac_toe.game import TicTacToe
from app.ui.window import Window
from test_session import Fake, wait_for


def window(qapp, tmp_path):
    controller = SessionController(
        TicTacToe(), Fake(), Config(directory=tmp_path, diagnostics=False))
    view = Window(controller)
    wait_for(qapp, lambda: controller.ready and not controller.busy)
    return controller, view


def test_startup_explanation_and_live_log_coexist(qapp, tmp_path):
    controller, view = window(qapp, tmp_path)
    text = view.diag.toPlainText()
    assert view.diag_button.isChecked() and not view.diag.isHidden()
    assert text.startswith('HOW THIS GAME WORKS\nTic-tac-toe · Quoted · ACTIVE')
    assert 'RULES\nX and O alternate' in text
    assert 'Recognition: imajev-4b-nf4' in text
    assert 'Opponent: imajev-4b-nf4' in text
    assert 'HOW DECISIONS ARE HANDLED' in text
    assert 'LIVE DIAGNOSTICS' in text and 'QUESTIONS ASKED (NEWEST FIRST)' in text
    view.close()


def test_mode_change_explains_active_and_pending_selection(qapp, tmp_path):
    controller, view = window(qapp, tmp_path)
    view.prompt_selector.setCurrentIndex(view.prompt_selector.findData('legacy'))
    text = view.diag.toPlainText()
    assert text.startswith('HOW THIS GAME WORKS\nTic-tac-toe · Original · ACTIVE')
    assert 'opening advice' in text
    assert controller.config.prompt_variant == 'legacy'
    controller.state = controller.game.apply_action(controller.state, 'place_A1')
    controller._publish()
    view.prompt_selector.setCurrentIndex(view.prompt_selector.findData('quoted'))
    text = view.diag.toPlainText()
    assert 'Tic-tac-toe · Quoted · PREVIEW — applies on New game' in text
    assert 'currently playing Original' in text
    assert controller.config.prompt_variant == 'legacy'
    view.close()


def test_game_change_preview_and_new_game_explanation(qapp, tmp_path):
    controller, view = window(qapp, tmp_path)
    controller.state = controller.game.apply_action(controller.state, 'place_A1')
    controller._publish()
    view.selector.setCurrentIndex(view.selector.findData('boku'))
    text = view.diag.toPlainText()
    assert 'Boku · Quoted · PREVIEW — applies on New game' in text
    assert 'Five or more adjacent stones' in text
    assert 'bounded search, not a proof of perfect play' in text
    assert controller.game.id == 'tic_tac_toe'
    view.new_game()
    assert controller.game.id == 'boku'
    assert 'Boku · Quoted · ACTIVE' in view.diag.toPlainText()
    view.close()


def test_diagnostics_scroll_position_survives_live_refresh(qapp, tmp_path):
    controller, view = window(qapp, tmp_path)
    bar = view.diag.verticalScrollBar()
    view.diag.resize(500, 160)
    controller.diagnostics = 'A' * 2000
    controller._publish()
    bar.setValue(min(5, bar.maximum()))
    position = bar.value()
    controller.diagnostics += ' more'
    controller._publish()
    assert bar.value() == position
    view.close()
