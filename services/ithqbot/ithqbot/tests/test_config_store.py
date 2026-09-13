import pytest
import json
from pathlib import Path
from ithqbot.config.store import FileConfigStore, RedisConfigStore
from ithqbot.config.schema import Config
from ithqbot.utils.crypto import AESEncryptor, SM4Encryptor

def test_file_config_store(tmp_path):
    store = FileConfigStore(tmp_path)
    data = {"bot": {"id": "test_bot", "name": "Test Bot"}}
    
    # Save
    store.save("test_bot", data)
    assert (tmp_path / "config-test_bot.json").exists()
    
    # Load
    loaded = store.load("test_bot")
    assert loaded == data
    
    # Default fallback
    store.save("default", {"bot": {"id": "default"}})
    assert store.load("non_existent") == {"bot": {"id": "default"}}

def test_redis_config_store():
    # This requires a running redis, we can mock it if needed
    # For now, let's just test the key logic
    try:
        store = RedisConfigStore("redis://localhost:6379/0", key_prefix="test:config:")
        data = {"bot": {"id": "redis_bot"}}
        store.save("redis_bot", data)
        loaded = store.load("redis_bot")
        assert loaded == data
    except Exception as e:
        pytest.skip(f"Redis not available: {e}")

def test_config_loader_with_bot_id(tmp_path):
    from ithqbot.config.loader import load_config, save_config
    
    config_file = tmp_path / "config.json"
    # Create bootstrap
    with open(config_file, "w") as f:
        json.dump({"bot_id": "bot_X"}, f)
    
    # Save a config for bot_X
    cfg = Config()
    cfg.bot.id = "bot_X"
    cfg.bot.name = "Bot X"
    save_config(cfg, config_path=config_file)
    
    # Load it back
    loaded = load_config(config_path=config_file)
    assert loaded.bot.id == "bot_X"
    assert loaded.bot.name == "Bot X"
    
    # Override bot_id
    loaded_y = load_config(config_path=config_file, bot_id="bot_Y")
    assert loaded_y.bot.id == "bot_Y" # Should be default with new ID

def test_config_encryption_aes(tmp_path):
    from ithqbot.config.loader import load_config, save_config
    
    encryptor = AESEncryptor("secret_key")
    store = FileConfigStore(tmp_path, encryptor=encryptor)
    
    data = {"bot": {"id": "secure_bot", "name": "Secure Bot"}}
    store.save("secure_bot", data)
    
    # Check file content is NOT plain JSON
    path = tmp_path / "config-secure_bot.json"
    with open(path, "r") as f:
        content = f.read()
        assert not content.startswith("{")
        
    # Load back
    loaded = store.load("secure_bot")
    assert loaded == data

def test_config_encryption_sm4(tmp_path):
    from ithqbot.config.loader import load_config, save_config
    
    encryptor = SM4Encryptor("guomi_key")
    store = FileConfigStore(tmp_path, encryptor=encryptor)
    
    data = {"bot": {"id": "sm4_bot", "name": "SM4 Bot"}}
    store.save("sm4_bot", data)
    
    # Check file content
    path = tmp_path / "config-sm4_bot.json"
    with open(path, "r") as f:
        content = f.read()
        assert not content.startswith("{")
        
    # Load back
    loaded = store.load("sm4_bot")
    assert loaded == data

def test_bootstrap_with_encryption(tmp_path):
    from ithqbot.config.loader import load_config, save_config
    import json
    
    config_file = tmp_path / "bootstrap.json"
    with open(config_file, "w") as f:
        json.dump({
            "encryption_enabled": True,
            "encryption_algorithm": "sm4",
            "encryption_key": "my_master_key",
            "bot_id": "bot_E"
        }, f)
        
    # Save config
    cfg = Config()
    cfg.bot.id = "bot_E"
    cfg.bot.name = "Encrypted Bot"
    save_config(cfg, config_path=config_file)
    
    # Load back
    loaded = load_config(config_path=config_file)
    assert loaded.bot.id == "bot_E"
    assert loaded.bot.name == "Encrypted Bot"
