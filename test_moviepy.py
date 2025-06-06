# test_moviepy.py
try:
    import moviepy
    print("Moviepy imported successfully")
    print("Version:", moviepy.__version__)
    
    import moviepy.editor
    print("Moviepy.editor imported successfully")
    
    from moviepy.editor import VideoFileClip
    print("VideoFileClip imported successfully")
except Exception as e:
    print(f"Error: {e}")
